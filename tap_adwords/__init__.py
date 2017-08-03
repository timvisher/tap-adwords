#!/usr/bin/env python3

import datetime
import os
import sys
import io
import csv
import time
import json
import copy

import xml.etree.ElementTree as ET
from suds.client import Client
import pendulum
import googleads
from googleads import adwords
from googleads import oauth2

import requests
import singer

from singer import utils
from singer import transform

LOGGER = singer.get_logger()
SESSION = requests.Session()
PAGE_SIZE = 100
VERSION = 'v201705'

REPORT_TYPE_MAPPINGS = {"Boolean":  {"type": ["null", "boolean"]},
                        "boolean":  {'type': ["null", "boolean"]},
                        "Double":   {"type": ["null", "number"]},
                        "int":      {"type": ["null", "integer"]},
                        "Integer":  {"type": ["null", "integer"]},
                        "long":     {"type": ["null", "integer"]},
                        "Long":     {"type": ["null", "integer"]},
                        "Date":     {"type": ["null", "string"],
                                     "format": "date-time"},
                        "DateTime": {"type": ["null", "string"],
                                     "format": "date-time"}}

GENERIC_ENDPOINT_MAPPINGS = {"campaigns": {'primary_keys': ["id"],
                                           'service_name': 'CampaignService'},
                             "ad_groups": {'primary_keys': ["id"],
                                           'service_name': 'AdGroupService'},
                             "ads":       {'primary_keys': ["adGroupId"],
                                           'service_name': 'AdGroupAdService'},
                             "accounts":  {'primary_keys': ["customerId"],
                                           'service_name': 'ManagedCustomerService'}}

UNSUPPORTED_REPORTS = frozenset([
    'UNKNOWN',
    # the following reports do not allow for querying by date range
    'CAMPAIGN_NEGATIVE_KEYWORDS_PERFORMANCE_REPORT',
    'CAMPAIGN_NEGATIVE_PLACEMENTS_PERFORMANCE_REPORT',
    'SHARED_SET_REPORT',
    'CAMPAIGN_SHARED_SET_REPORT',
    'SHARED_SET_CRITERIA_REPORT',
    'BUDGET_PERFORMANCE_REPORT',
    'CAMPAIGN_NEGATIVE_LOCATIONS_REPORT',
    'LABEL_REPORT',
])

REPORTS_WITH_90_DAY_MAX = frozenset([
    'CLICK_PERFORMANCE_REPORT',
])

REQUIRED_CONFIG_KEYS = [
    "start_date",
    "oauth_client_id",
    "oauth_client_secret",
    "user_agent",
    "refresh_token",
    "customer_ids",
    "developer_token",
]

CONFIG = {}
STATE = {}

def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)

def load_schema(entity):
    return utils.load_json(get_abs_path("schemas/{}.json".format(entity)))

def get_start(key):
    if key not in STATE:
        STATE[key] = CONFIG['start_date']

    return STATE[key]

def state_key_name(customer_id, report_name):
    return report_name + "_" + customer_id

def should_sync(discovered_schema, annotated_schema, field):
    return annotated_schema['properties'][field].get('selected') \
        or discovered_schema['properties'][field].get('inclusion') == 'automatic'

def get_fields_to_sync(discovered_schema, annotated_schema):
    fields = annotated_schema['properties'] # pylint: disable=unsubscriptable-object
    return [field for field in fields if should_sync(discovered_schema, annotated_schema, field)]


def strip_inclusion(dic):
    dic.pop("inclusion", None)
    for val in dic.values():
        if isinstance(val, dict):
            strip_inclusion(val)

def write_schema(stream_name, schema, primary_keys):
    schema_copy = copy.deepcopy(schema)
    strip_inclusion(schema_copy)
    singer.write_schema(stream_name, schema_copy, primary_keys)

# No rate limit here, since this request is only made once
# per discovery (not sync) job
def request_xsd(url):
    req = requests.Request("GET", url=url).prepare()
    LOGGER.info("GET {}".format(req.url))
    resp = SESSION.send(req)

    return resp.text

def sync_report(stream_name, annotated_stream_schema, sdk_client):
    customer_id = sdk_client.client_customer_id

    stream_schema = create_schema_for_report(stream_name, sdk_client)
    xml_attribute_list = get_fields_to_sync(stream_schema, annotated_stream_schema)
    primary_keys = ['customer_id', 'day', '_sdc_id']
    LOGGER.info("{} primary keys are {}".format(stream_name, primary_keys))
    write_schema(stream_name, stream_schema, primary_keys)

    field_list = []
    for field in xml_attribute_list:
        if field != 'customer_id':
            field_list.append(stream_schema['properties'][field]['field'])

    check_selected_fields(stream_name, field_list, sdk_client)

    start_date = pendulum.parse(get_start(state_key_name(customer_id, stream_name)))
    if stream_name in REPORTS_WITH_90_DAY_MAX:
        cutoff = pendulum.utcnow().subtract(days=90)
        if start_date < cutoff:
            start_date = cutoff

    LOGGER.info('Selected fields: %s', field_list)

    while start_date <= pendulum.now():
        sync_report_for_day(stream_name, stream_schema, sdk_client, start_date, field_list)
        start_date = start_date.add(days=1)
    LOGGER.info("Done syncing the %s report for customer_id %s", stream_name, customer_id)

def parse_csv_string(csv_string):
    string_buffer = io.StringIO(csv_string)
    reader = csv.reader(string_buffer)
    rows = [row for row in reader]

    headers = rows[0]
    values = rows[1:]
    return headers, values

def get_xml_attribute_headers(stream_schema, description_headers):
    description_to_xml_attribute = {}
    for key, value in stream_schema['properties'].items():
        description_to_xml_attribute[value['description']] = key
    description_to_xml_attribute['Ad policies'] = 'policy'

    xml_attribute_headers = [description_to_xml_attribute[header] for header in description_headers]
    return xml_attribute_headers

def transform_pre_hook(data, typ, schema): # pylint: disable=unused-argument
    if isinstance(data, str) and '--' in data:
        data = None

    elif data and typ == "number":
        if data == "> 90%":
            data = "90.01"

        if data == "< 10%":
            data = "9.99"

        if data.endswith(" x"):
            data = data[:-2]

        data = data.replace('%', '')

    return data

def sync_report_for_day(stream_name, stream_schema, sdk_client, start, field_list):
    report_downloader = sdk_client.GetReportDownloader(version=VERSION)
    customer_id = sdk_client.client_customer_id
    report = {
        'reportName': 'Seems this is required',
        'dateRangeType': 'CUSTOM_DATE',
        'reportType': stream_name,
        'downloadFormat': 'CSV',
        'selector': {
            'fields': field_list,
            'dateRange': {'min': start.strftime('%Y%m%d'),
                          'max': start.strftime('%Y%m%d')}}}

    # Fetch the report as a csv string
    result = report_downloader.DownloadReportAsString(
        report, skip_report_header=True, skip_column_header=False,
        skip_report_summary=True,
        # Do not get data with 0 impressions, because some reports don't support that
        include_zero_impressions=False)

    headers, values = parse_csv_string(result)
    i = 0
    for i, val in enumerate(values):
        obj = dict(zip(get_xml_attribute_headers(stream_schema, headers), val))
        obj['customer_id'] = customer_id
        obj = transform(obj, stream_schema,
                        integer_datetime_fmt=singer.UNIX_SECONDS_INTEGER_DATETIME_PARSING,
                        pre_hook=transform_pre_hook)
        obj['_sdc_id'] = i

        singer.write_record(stream_name, obj)

    utils.update_state(STATE, state_key_name(customer_id, stream_name), start)
    singer.write_state(STATE)
    LOGGER.info("Done syncing %s records for the %s report for customer_id %s on %s",
                i, stream_name, customer_id, start)

def suds_to_dict(obj):
    if not hasattr(obj, '__keylist__'):
        return obj
    data = {}
    fields = obj.__keylist__
    for field in fields:
        val = getattr(obj, field)
        if isinstance(val, list):
            data[field] = []
            for item in val:
                data[field].append(suds_to_dict(item))
        else:
            data[field] = suds_to_dict(val)
    return data

CAMPAIGNS_BLACK_LISTED_FIELDS = set(['networkSetting', 'conversionOptimizerEligibility',
                                     'frequencyCap'])
AD_GROUPS_BLACK_LISTED_FIELDS = set(['biddingStrategyConfiguration'])

def filter_fields_by_stream_name(stream_name, fields_to_sync):
    if stream_name == 'campaigns':
        return [f for f in fields_to_sync if f not in CAMPAIGNS_BLACK_LISTED_FIELDS]
    elif stream_name == 'ad_groups':
        return [f for f in fields_to_sync if f not in AD_GROUPS_BLACK_LISTED_FIELDS]
    elif stream_name == 'ads':
        return fields_to_sync
    elif stream_name == 'accounts':
        return fields_to_sync
    else:
        raise Exception("unrecognized generic stream_name {}".format(stream_name))

def sync_generic_endpoint(stream_name, annotated_stream_schema, sdk_client):
    customer_id = sdk_client.client_customer_id
    discovered_schema = load_schema(stream_name)
    service_name = GENERIC_ENDPOINT_MAPPINGS[stream_name]['service_name']
    primary_keys = GENERIC_ENDPOINT_MAPPINGS[stream_name]['primary_keys']
    write_schema(stream_name, discovered_schema, primary_keys)

    service_caller = sdk_client.GetService(service_name, version=VERSION)
    LOGGER.info("Syncing %s", stream_name)
    field_list = get_fields_to_sync(discovered_schema, annotated_stream_schema)
    LOGGER.info("Request fields: %s", field_list)
    field_list = filter_fields_by_stream_name(stream_name, field_list)
    LOGGER.info("Filtered fields: %s", field_list)

    field_list = [f[0].upper()+f[1:] for f in field_list]

    offset = 0
    selector = {
        'fields': field_list,
        'paging': {
            'startIndex': str(offset),
            'numberResults': str(PAGE_SIZE)
        },
    }

    more_pages = True
    while more_pages:
        page = service_caller.get(selector)
        if 'entries' in page:
            for entry in page['entries']:
                record = transform(suds_to_dict(entry), discovered_schema,
                                   integer_datetime_fmt=singer.UNIX_SECONDS_INTEGER_DATETIME_PARSING, # pylint: disable=line-too-long
                                   pre_hook=transform_pre_hook)
                singer.write_record(stream_name, record)
        offset += PAGE_SIZE
        selector['paging']['startIndex'] = str(offset)
        more_pages = offset < int(page['totalNumEntries'])
    LOGGER.info("Done syncing %s for customer_id %s", stream_name, customer_id)

def sync_stream(stream, annotated_schema, sdk_client):
    if stream in GENERIC_ENDPOINT_MAPPINGS:
        sync_generic_endpoint(stream, annotated_schema, sdk_client)
    else:
        sync_report(stream, annotated_schema, sdk_client)

def do_sync(annotated_schema, sdk_client):
    for stream in annotated_schema['streams']:
        stream_name = stream.get('stream')
        stream_schema = stream.get('schema')
        if stream_schema.get('selected'):
            LOGGER.info('Syncing stream %s ...', stream_name)
            sync_stream(stream_name, stream_schema, sdk_client)

        else:
            LOGGER.info('Skipping stream %s.', stream_name)

def get_report_definition_service(report_type, sdk_client):
    report_definition_service = sdk_client.GetService(
        'ReportDefinitionService', version=VERSION)
    fields = report_definition_service.getReportFields(report_type)
    return fields

def create_type_map(typ):
    if REPORT_TYPE_MAPPINGS.get(typ):
        return REPORT_TYPE_MAPPINGS.get(typ)
    return {'type' : ['null', 'string']}

def create_schema_for_report(stream, sdk_client):
    report_properties = {}
    LOGGER.info('Loading schema for %s', stream)
    fields = get_report_definition_service(stream, sdk_client)
    for field in fields:
        report_properties[field['xmlAttributeName']] = {'description': field['displayFieldName'],
                                                        'behavior': field['fieldBehavior'],
                                                        'field': field['fieldName'],
                                                        'inclusion': "available"}
        report_properties[field['xmlAttributeName']].update(create_type_map(field['fieldType']))
        if field['xmlAttributeName'] == 'day':
            report_properties['day']['inclusion'] = 'automatic'

    report_properties['customer_id'] = {'description': 'Profile ID',
                                        'behavior': 'ATTRIBUTE',
                                        'type': 'string',
                                        'field': "customer_id",
                                        'inclusion': 'automatic'}
    if stream == 'AD_PERFORMANCE_REPORT':
        # The data for this field is "image/jpeg" etc. However, the
        # discovered schema from the report description service claims
        # that it should be an integer. This is needed to correct that.
        report_properties['imageMimeType'] = {
            'description': 'Image Mime Type',
            'behavior': 'ATTRIBUTE',
            'type': ['null', 'string'],
            'field': 'ImageCreativeMimeType',
        }


    return {"type": "object",
            "is_report": 'true',
            "properties": report_properties,
            "inclusion": "available"}

def check_selected_fields(stream, field_list, sdk_client):
    field_set = set(field_list)
    fields = get_report_definition_service(stream, sdk_client)
    field_map = {f.fieldName: f.xmlAttributeName for f in fields}
    errors = []
    for field in fields:
        if field.fieldName not in field_set:
            continue

        if not hasattr(field, "exclusiveFields"):
            continue

        field_errors = []
        for ex_field in field.exclusiveFields:
            if ex_field in field_set:
                field_errors.append(field_map[ex_field])

        if field_errors:
            errors.append("{} cannot be selected with {}".format(
                field.xmlAttributeName, ",".join(field_errors)))

    if errors:
        raise Exception("Fields selection violates Google's exclusion rules:\n\t{}" \
                        .format("\n\t".join(errors)))

def do_discover_reports(sdk_client):
    url = 'https://adwords.google.com/api/adwords/reportdownload/{}/reportDefinition.xsd'.format(VERSION)
    xsd = request_xsd(url)
    root = ET.fromstring(xsd)
    nodes = list(root.find(".//*[@name='ReportDefinition.ReportType']/*"))

    stream_names = [p.attrib['value'] for p in nodes if p.attrib['value'] not in UNSUPPORTED_REPORTS] #pylint: disable=line-too-long
    streams = []
    LOGGER.info("Starting report discovery")
    for stream_name in stream_names:
        streams.append({'stream': stream_name,
                        'tap_stream_id': stream_name,
                        'schema': create_schema_for_report(stream_name, sdk_client)})
    LOGGER.info("Report discovery complete")
    return streams

def do_discover_generic_endpoints():
    streams = []
    LOGGER.info("Starting generic discovery")
    for stream_name in GENERIC_ENDPOINT_MAPPINGS:
        LOGGER.info('Loading schema for %s', stream_name)
        streams.append({'stream': stream_name,
                        'tap_stream_id': stream_name,
                        'schema': load_schema(stream_name)})
    LOGGER.info("Generic discovery complete")
    return streams

def do_discover(customer_ids):
    sdk_client = create_sdk_client(customer_ids[0])
    generic_streams = do_discover_generic_endpoints()
    report_streams = do_discover_reports(sdk_client)
    streams = []
    streams.extend(generic_streams)
    streams.extend(report_streams)
    json.dump({"streams": streams}, sys.stdout, indent=2)

def create_sdk_client(customer_id):
    oauth2_client = oauth2.GoogleRefreshTokenClient(
        CONFIG['oauth_client_id'], \
        CONFIG['oauth_client_secret'], \
        CONFIG['refresh_token'])

    sdk_client = adwords.AdWordsClient(CONFIG['developer_token'], \
                                 oauth2_client, user_agent=CONFIG['user_agent'], \
                                 client_customer_id=customer_id)
    return sdk_client

def do_sync_all_customers(customer_ids, properties):
    for customer_id in customer_ids:
        LOGGER.info('Syncing customer ID %s ...', customer_id)
        sdk_client = create_sdk_client(customer_id)
        do_sync(properties, sdk_client)
        LOGGER.info('Done syncing customer ID %s.', customer_id)

def main():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    CONFIG.update(args.config)
    STATE.update(args.state)
    customer_ids = CONFIG['customer_ids'].split(",")

    if args.discover:
        do_discover(customer_ids)
        LOGGER.info("Discovery complete")
    elif args.properties:
        do_sync_all_customers(customer_ids, args.properties)
        LOGGER.info("Sync Completed")
    else:
        LOGGER.info("No properties were selected")

if __name__ == "__main__":
    main()
