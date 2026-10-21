"""End-to-end test for the Real Estate Speed-to-Lead Router against MOCK APIs.

No real monday.com board, Twilio number, Claude account or Slack workspace is used. The script:
  1. starts mock monday.com (GraphQL), Twilio, Anthropic and Slack APIs on this machine,
  2. imports temporary copies of the workflow and the error alert workflow into your n8n
     (API hosts pointed at the mocks, fake keys, a 9 second escalation timer),
  3. sends leads to the webhook: forged and empty requests, website forms, portal JSON, form posts,
     duplicates (one after another and at the same moment), outages of Claude, monday, Twilio and Slack,
     a Claude refusal, a Canadian number and lead text that carries Slack markup,
  4. checks every request the workflow made, the nodes each run executed and the node it ended on, and matches
     every error alert to its run by the execution link; prints PASS or FAIL per check,
     then deletes the temporary workflows.

The mock monday API validates every query and mutation against a GraphQL schema written from monday's API
reference (field names, argument types, variable types), checks column values the way monday does
(phone needs countryShortName and a number that belongs to that country, status labels must exist unless
create_labels_if_missing is set, unknown column IDs fail) and returns errors with HTTP 200 like the real API.
It also honours the Idempotency-Key header: a repeat gets the cached response with Idempotency-Replayed: true,
a repeat while the first is still running gets 409. Any request monday would reject fails the run.
The mock Twilio API wants basic auth and a form encoded body. The mock Claude returns scripted tool calls but rejects
requests the real Messages API would reject (unknown fields, an effort setting on Haiku, a tool that is not forced).

Requirements: Python 3.9+, graphql-core (pip install graphql-core), a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
The mocks listen on 127.0.0.1:8783. Every temporary workflow is named "TEST real-estate-speed-to-lead (temporary)"
and its webhook path starts with "real-estate-speed-to-lead-e2e".
Usage: python tests/e2e_mock_test.py   (takes about 5 minutes, most of it waiting for escalation timers)
"""
import base64, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from graphql import GraphQLError, OperationType, build_schema, execute, get_operation_ast, parse, validate
except ImportError:
    raise SystemExit('This test needs graphql-core for the mock monday API: pip install graphql-core')

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8783
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / 'workflow' / 'real-estate-speed-to-lead.json'
ERROR_WORKFLOW = ROOT / 'workflow' / 'error-alert-slack.json'
SLUG = 'real-estate-speed-to-lead'
TEMP_NAME = f'TEST {SLUG} (temporary)'   # every temporary workflow gets this exact name
HOSTS = {'https://api.anthropic.com': MOCK, 'https://api.monday.com': MOCK, 'https://api.twilio.com': MOCK}

SECRET = 'test-lead-secret'
MONDAY_TOKEN = 'mock-monday-token'
MONDAY_VERSION = '2026-07'
BOARD, BOARD_C = '5551234567', '5557654321'
TWILIO_SID = 'AC' + 'f' * 32
TWILIO_TOKEN = 'mock-twilio-token'
TWILIO_FROM = '+15125550100'
MESSAGING_SERVICE = 'MG' + 'e' * 32
MAYA, LEO, ANA = '+15125550101', '+15125550102', '+15125550103'
SLACK_IDS = {'Maya Chen': 'U01MAYA0001', 'Leo Park': 'U01LEO00002', 'Ana Diaz': 'U01ANA00003'}
BROKER = 'U01BROKER01'
MINUTES = 0.15   # 9 second escalation timer


def agents(*away):
    return json.dumps([
        {'name': 'Maya Chen', 'phone': MAYA, 'slack': SLACK_IDS['Maya Chen'], 'email': 'maya@example.com', 'away': 'Maya Chen' in away},
        {'name': 'Ana Diaz', 'phone': ANA, 'slack': SLACK_IDS['Ana Diaz'], 'email': 'ana@example.com', 'away': True},
        {'name': 'Leo Park', 'phone': LEO, 'slack': SLACK_IDS['Leo Park'], 'email': 'leo@example.com', 'away': 'Leo Park' in away},
    ])


BASE_ENV = {
    'LEAD_WEBHOOK_SECRET': SECRET, 'BUSINESS_NAME': 'Acme Realty',
    'ANTHROPIC_API_KEY': 'mock-anthropic-key', 'ANTHROPIC_MODEL': 'claude-sonnet-5',
    'MONDAY_API_TOKEN': MONDAY_TOKEN, 'MONDAY_API_VERSION': MONDAY_VERSION, 'MONDAY_BOARD_ID': BOARD, 'MONDAY_GROUP_ID': 'new_leads',
    'MONDAY_COLUMNS': '',
    'TWILIO_ACCOUNT_SID': TWILIO_SID, 'TWILIO_AUTH_TOKEN': TWILIO_TOKEN, 'TWILIO_FROM_NUMBER': TWILIO_FROM, 'TWILIO_MESSAGING_SERVICE_SID': '',
    'AGENTS_JSON': agents(), 'SLACK_WEBHOOK_URL': MOCK + '/slack/hook', 'BROKER_SLACK_ID': BROKER,
    'DEDUPE_HOURS': '72', 'ESCALATION_MINUTES': str(MINUTES), 'QUIET_HOURS': '', 'TIMEZONE': 'America/Chicago', 'DEFAULT_COUNTRY': 'US',
}
LOG = []
LOG_LOCK = threading.Lock()


def log(entry):
    with LOG_LOCK:
        entry['n'] = len(LOG)
        entry['t'] = time.time()
        LOG.append(entry)


# ------------------------------------------------------------------ mock monday.com (GraphQL)
# Written from developer.monday.com: items, items_page_by_column_values, create_item, change_multiple_column_values,
# create_update, column values (phone, email, status, text, long text, numbers). Only the parts this workflow uses.
SDL = '''
scalar JSON
scalar Date
schema { query: Query mutation: Mutation }
type Query {
  items(ids: [ID!], limit: Int, page: Int, newest_first: Boolean, exclude_nonactive: Boolean): [Item]
  items_page_by_column_values(board_id: ID!, columns: [ItemsPageByColumnValuesQuery!], limit: Int! = 25, cursor: String, hierarchy_scope_config: String): ItemsResponse!
  me: User
}
input ItemsPageByColumnValuesQuery { column_id: String!, column_values: [String] }
type ItemsResponse { cursor: String, items: [Item!]! }
type User { id: ID!, name: String! }
type Board { id: ID!, name: String! }
type Group { id: ID!, title: String! }
enum State { active all archived deleted }
enum ColumnType { phone email status text long_text numbers }
enum PositionRelative { before_at after_at }
enum MentionType { User Team Board Project }
input UpdateMention { id: ID!, type: MentionType! }
type Column { id: ID!, title: String!, type: ColumnType! }
interface ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column! }
type PhoneValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column!, phone: String, country_short_name: String }
type EmailValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column!, email: String, label: String }
type StatusValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column!, label: String, index: Int, is_done: Boolean }
type TextValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column! }
type LongTextValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column! }
type NumbersValue implements ColumnValue { id: ID!, text: String, value: JSON, type: ColumnType!, column: Column!, number: Float }
type Item {
  id: ID!
  name: String!
  created_at: Date
  updated_at: Date
  state: State
  url: String!
  relative_link: String
  board: Board
  group: Group
  column_values(ids: [String!], types: [ColumnType!]): [ColumnValue!]!
}
type Update { id: ID!, body: String!, text_body: String, created_at: Date }
type Mutation {
  create_item(board_id: ID!, item_name: String!, group_id: String, column_values: JSON, create_labels_if_missing: Boolean, position_relative_method: PositionRelative, relative_to: ID): Item
  change_multiple_column_values(item_id: ID, board_id: ID!, column_values: JSON!, create_labels_if_missing: Boolean): Item
  create_update(item_id: ID, body: String!, parent_id: ID, mentions_list: [UpdateMention], original_creation_date: String): Update
  archive_item(item_id: ID): Item
  delete_item(item_id: ID): Item
}
'''
SCHEMA = build_schema(SDL)
TYPENAMES = {'phone': 'PhoneValue', 'email': 'EmailValue', 'status': 'StatusValue', 'text': 'TextValue', 'long_text': 'LongTextValue', 'numbers': 'NumbersValue'}
BAD_VALUE = 'invalid value, please check our API documentation for the correct data structure for this column. https://developer.monday.com/api-reference/docs/change-column-values'


def gerr(code, message, data=None):
    return GraphQLError(message, extensions={'code': code, 'error_data': data or {}, 'status_code': 200})


# monday validates a phone against its country with libphonenumber. +1 is shared by the US, Canada, US territories
# and the Caribbean, so for +1 numbers the area code decides which country is right.
NANP_CA = set('204 226 236 249 250 257 263 289 306 343 354 365 367 368 382 403 416 418 428 431 437 438 450 468 474 506 514 519 548 579 '
              '581 584 587 604 613 639 647 672 683 705 709 742 753 778 780 782 807 819 825 867 873 879 902 905 942'.split())
NANP_OTHER = {'787': 'PR', '939': 'PR', '340': 'VI', '671': 'GU', '670': 'MP', '684': 'AS', '242': 'BS', '246': 'BB', '264': 'AI', '268': 'AG',
              '284': 'VG', '345': 'KY', '441': 'BM', '473': 'GD', '649': 'TC', '658': 'JM', '876': 'JM', '664': 'MS', '721': 'SX', '758': 'LC',
              '767': 'DM', '784': 'VC', '809': 'DO', '829': 'DO', '849': 'DO', '868': 'TT', '869': 'KN'}


def phone_country_ok(phone, country):
    digits = phone.lstrip('+')
    if digits.startswith('1'):
        area = digits[1:4]
        return country == ('CA' if area in NANP_CA else NANP_OTHER.get(area, 'US'))
    calling = {'GB': '44', 'AU': '61', 'IE': '353', 'NZ': '64'}
    return country not in calling or digits.startswith(calling[country])


def json_in(value):
    """monday's column_values argument takes a JSON encoded string."""
    if not isinstance(value, str):
        raise gerr('ColumnValueException', 'column_values must be a JSON encoded string')
    try:
        return json.loads(value)
    except ValueError:
        raise gerr('JsonParseException', 'The column_values JSON could not be parsed')


SCHEMA.type_map['ColumnValue'].resolve_type = lambda obj, info, t: obj['__typename']


class Monday:
    def __init__(self):
        self.lock = threading.RLock()
        self.boards, self.items, self.updates = {}, {}, []
        self.next_id = 9100000001
        self.idem, self.inflight = {}, set()
        self.fail_500, self.fail_gql, self.fail_409 = {}, {}, {}

    def add_board(self, board_id, columns, groups=('topics', 'new_leads')):
        self.boards[board_id] = {'columns': columns, 'labels': {c: ['Working on it', 'Done', 'Stuck'] for c, t in columns.items() if t == 'status'},
                                 'groups': list(groups)}

    # column values, checked like monday checks them
    def check_value(self, board, col, ctype, val, create_labels):
        if ctype == 'phone':
            ok = isinstance(val, dict) and isinstance(val.get('phone'), str) and re.fullmatch(r'\+?\d{7,15}', val['phone'] or '') \
                and isinstance(val.get('countryShortName'), str) and re.fullmatch(r'[A-Z]{2}', val['countryShortName'])
            if ok and not phone_country_ok(val['phone'], val['countryShortName']):
                ok = False   # libphonenumber rejects a number that does not belong to the country
            if not ok:
                raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
            return {'phone': val['phone'], 'countryShortName': val['countryShortName']}
        if ctype == 'email':
            if not (isinstance(val, dict) and isinstance(val.get('email'), str) and re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', val['email'])
                    and isinstance(val.get('text'), str)):
                raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
            return {'email': val['email'], 'text': val['text']}
        if ctype == 'status':
            labels = board['labels'][col]
            if isinstance(val, dict) and isinstance(val.get('label'), str):
                if val['label'] not in labels:
                    if not create_labels:
                        raise gerr('ColumnValueException', f"This status label doesn't exist, possible statuses are: {labels}", {'column_id': col})
                    labels.append(val['label'])
                return {'label': val['label']}
            if isinstance(val, dict) and isinstance(val.get('index'), int) and 0 <= val['index'] < len(labels):
                return {'label': labels[val['index']]}
            raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
        if ctype == 'text':
            if not isinstance(val, str):
                raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
            return val
        if ctype == 'long_text':
            if not (isinstance(val, dict) and isinstance(val.get('text'), str) and len(val['text']) <= 2000):
                raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
            return {'text': val['text']}
        if ctype == 'numbers':
            if not (isinstance(val, str) and re.fullmatch(r'-?\d+(\.\d+)?|', val)):
                raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})
            return val
        raise gerr('ColumnValueException', BAD_VALUE, {'column_id': col})

    def apply(self, board, values, updates, create_labels):
        if not isinstance(updates, dict):
            raise gerr('ColumnValueException', 'column_values must be a JSON object')
        staged = dict(values)
        for col, val in updates.items():
            ctype = board['columns'].get(col)
            if ctype is None:
                raise gerr('InvalidColumnIdException', "This column ID doesn't exist for the board", {'column_id': col})
            staged[col] = self.check_value(board, col, ctype, val, create_labels)
        values.clear()
        values.update(staged)

    def column_value(self, board_id, col, stored):
        ctype = self.boards[board_id]['columns'][col]
        cv = {'__typename': TYPENAMES[ctype], 'id': col, 'type': ctype, 'column': {'id': col, 'title': col, 'type': ctype}, 'text': '', 'value': None}
        if stored is None or stored == '':
            return cv
        if ctype == 'phone':
            cv.update(text=stored['phone'], value=json.dumps({'phone': stored['phone'], 'countryShortName': stored['countryShortName']}),
                      phone=stored['phone'], country_short_name=stored['countryShortName'])
        elif ctype == 'email':
            text = stored['email'] if stored['text'] in ('', stored['email']) else f"{stored['text']} - {stored['email']}"
            cv.update(text=text, value=json.dumps({'email': stored['email'], 'text': stored['text'], 'changed_at': '2026-10-20T10:00:00.000Z'}),
                      email=stored['email'], label=stored['text'])
        elif ctype == 'status':
            index = self.boards[board_id]['labels'][col].index(stored['label'])
            cv.update(text=stored['label'], value=json.dumps({'index': index}), label=stored['label'], index=index, is_done=stored['label'] == 'Done')
        elif ctype == 'long_text':
            cv.update(text=stored['text'], value=json.dumps({'text': stored['text']}))
        else:
            cv.update(text=stored, value=json.dumps(stored))
        return cv

    def item_view(self, item_id):
        it = self.items[item_id]
        return {'id': item_id, 'name': it['name'], 'created_at': it['created_at'], 'updated_at': it['created_at'], 'state': it['state'],
                'url': f"https://example-realty.monday.com/boards/{it['board']}/pulses/{item_id}", 'board': {'id': it['board'], 'name': 'Leads'},
                'group': {'id': it['group'], 'title': it['group']}, '_board': it['board']}

    def find(self, phone):
        return [i for i, it in self.items.items() if (it['values'].get('lead_phone') or {}).get('phone') == phone]


MON = Monday()
MON.add_board(BOARD, {'lead_phone': 'phone', 'lead_email': 'email', 'lead_status': 'status', 'lead_agent': 'text', 'lead_source': 'text',
                      'lead_intent': 'text', 'lead_budget': 'text', 'lead_timeline': 'text', 'lead_area': 'text', 'lead_preapproved': 'text',
                      'lead_urgency': 'numbers', 'lead_summary': 'text', 'lead_message': 'long_text', 'lead_classified_by': 'text', 'lead_run': 'text'})
# Board C uses different IDs for two columns, to prove MONDAY_COLUMNS works
MON.add_board(BOARD_C, {'lead_phone': 'phone', 'lead_email': 'email', 'status': 'status', 'text_agent': 'text', 'lead_source': 'text',
                        'lead_intent': 'text', 'lead_budget': 'text', 'lead_timeline': 'text', 'lead_area': 'text', 'lead_preapproved': 'text',
                        'lead_urgency': 'numbers', 'lead_summary': 'text', 'lead_message': 'long_text', 'lead_classified_by': 'text', 'lead_run': 'text'})


def r_items(_, info, ids=None, **kw):
    return [MON.item_view(str(i)) for i in (ids or []) if str(i) in MON.items]


def r_page(_, info, board_id, columns=None, limit=25, **kw):
    board = MON.boards.get(str(board_id))
    if board is None:
        raise gerr('InvalidBoardIdException', 'The board does not exist', {'board_id': board_id})
    out = []
    for item_id, it in MON.items.items():
        if it['board'] != str(board_id) or it['state'] != 'active':
            continue
        keep = True
        for rule in columns or []:
            col, wanted = rule['column_id'], [w for w in (rule.get('column_values') or []) if w is not None]
            ctype = board['columns'].get(col)
            if ctype is None:
                raise gerr('InvalidColumnIdException', "This column ID doesn't exist for the board", {'column_id': col})
            stored = it['values'].get(col)
            if ctype == 'phone':   # one full or partial number, digits only
                keep = keep and bool(stored) and len(wanted) == 1 and wanted[0].isdigit() and wanted[0] in stored['phone'].lstrip('+')
            elif ctype == 'email':  # exact match of the whole label
                keep = keep and bool(stored) and stored['text'] in wanted
            else:
                keep = keep and stored in wanted
        if keep:
            out.append(MON.item_view(item_id))
    return {'cursor': None, 'items': out[:limit]}


def r_column_values(item, info, ids=None, types=None):
    stored = MON.items[item['id']]['values']
    cols = MON.boards[item['_board']]['columns']
    wanted = ids if ids is not None else list(cols)
    return [MON.column_value(item['_board'], c, stored.get(c)) for c in wanted if c in cols]


def r_create_item(_, info, board_id, item_name, group_id=None, column_values=None, create_labels_if_missing=False, **kw):
    board = MON.boards.get(str(board_id))
    if board is None:
        raise gerr('InvalidBoardIdException', 'The board does not exist', {'board_id': board_id})
    if group_id is not None and group_id not in board['groups']:
        raise gerr('InvalidGroupIdException', 'The group does not exist', {'group_id': group_id})
    if not item_name.strip() or len(item_name) > 255:
        raise gerr('ItemNameTooLongException', 'Item name must be between 1 and 255 characters')
    values = {}
    MON.apply(board, values, json_in(column_values) if column_values is not None else {}, create_labels_if_missing)
    item_id = str(MON.next_id)
    MON.next_id += 1
    MON.items[item_id] = {'board': str(board_id), 'name': item_name, 'group': group_id or board['groups'][0], 'state': 'active',
                          'created_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'values': values}
    return MON.item_view(item_id)


def r_change(_, info, board_id, column_values, item_id=None, create_labels_if_missing=False):
    it = MON.items.get(str(item_id))
    if it is None or it['board'] != str(board_id):
        raise gerr('ResourceNotFoundException', 'Item not found in board', {'item_id': item_id})
    MON.apply(MON.boards[it['board']], it['values'], json_in(column_values), create_labels_if_missing)
    return MON.item_view(str(item_id))


def r_update(_, info, body, item_id=None, **kw):
    if str(item_id) not in MON.items:
        raise gerr('ResourceNotFoundException', 'Item not found', {'item_id': item_id})
    if not body.strip():
        raise gerr('InvalidArgumentException', 'Update body cannot be empty')
    MON.updates.append({'item': str(item_id), 'body': body})
    return {'id': str(800000 + len(MON.updates)), 'body': body, 'text_body': body, 'created_at': '2026-10-20T10:00:00Z'}


q, m = SCHEMA.query_type.fields, SCHEMA.mutation_type.fields
q['items'].resolve, q['items_page_by_column_values'].resolve = r_items, r_page
m['create_item'].resolve, m['change_multiple_column_values'].resolve, m['create_update'].resolve = r_create_item, r_change, r_update
SCHEMA.type_map['Item'].fields['column_values'].resolve = r_column_values


def monday_api(headers, raw):
    """Returns (status, body, extra headers, log details). Every request monday would reject is logged with invalid=True."""
    if headers.get('authorization') != MONDAY_TOKEN:
        return 401, {'errors': [{'message': 'Not Authenticated', 'extensions': {'code': 'Unauthorized', 'status_code': 401}}]}, {}, {'invalid': 'auth'}
    if 'application/json' not in headers.get('content-type', ''):
        return 400, {'errors': [{'message': 'Bad request', 'extensions': {'status_code': 400}}]}, {}, {'invalid': 'content type'}
    try:
        body = json.loads(raw)
    except ValueError:
        return 400, {'errors': [{'message': 'JsonParseException', 'extensions': {'code': 'JsonParseException', 'status_code': 400}}]}, {}, {'invalid': 'json'}
    if headers.get('api-version') != MONDAY_VERSION:
        return 200, {'errors': [{'message': 'The requested API version is invalid', 'extensions': {'code': 'InvalidVersionException', 'status_code': 200}}]}, {}, \
            {'invalid': 'version'}
    query, variables = body.get('query'), body.get('variables') or {}
    try:
        doc = parse(query if isinstance(query, str) else '')
    except GraphQLError as e:
        return 200, {'errors': [{'message': f'Parse error on query: {e.message}', 'extensions': {'code': 'PARSE_ERROR', 'status_code': 200}}]}, {}, \
            {'invalid': 'parse'}
    problems = validate(SCHEMA, doc)
    if problems:
        return 200, {'errors': [p.formatted for p in problems], 'account_id': 4242}, {}, {'invalid': 'schema'}
    op = get_operation_ast(doc)
    roots = [s.name.value for s in op.selection_set.selections]
    detail = {'op': op.name.value if op.name else '', 'roots': roots, 'variables': variables, 'mutation': op.operation == OperationType.MUTATION}
    for root in roots:
        if detail['mutation'] and headers.get('idempotency-key') and MON.fail_409.get(root, 0) > 0:
            MON.fail_409[root] -= 1   # the same key is "still being processed" on monday's side
            return 409, {'errors': [{'message': 'A request with this idempotency key is currently being processed',
                         'extensions': {'code': 'IDEMPOTENCY_CONFLICT', 'status_code': 409}}]}, {'Retry-After': '1'}, dict(detail, conflict=True)
        if MON.fail_500.get(root, 0) > 0:
            MON.fail_500[root] -= 1
            return 500, {'error_message': 'Internal server error', 'status_code': 500}, {}, detail
        if MON.fail_gql.get(root, 0) > 0:
            MON.fail_gql[root] -= 1
            return 200, {'data': {root: None}, 'errors': [{'message': BAD_VALUE, 'path': [root],
                         'extensions': {'code': 'ColumnValueException', 'error_data': {}, 'status_code': 200}}], 'account_id': 4242}, {}, detail
    key = headers.get('idempotency-key') if detail['mutation'] else None
    if key:
        with MON.lock:
            if key in MON.idem:
                return 200, MON.idem[key], {'Idempotency-Replayed': 'true'}, dict(detail, replayed=True)
            if key in MON.inflight:
                return 409, {'errors': [{'message': 'A request with this idempotency key is currently being processed',
                             'extensions': {'code': 'IDEMPOTENCY_CONFLICT', 'status_code': 409}}]}, {'Retry-After': '1'}, dict(detail, conflict=True)
            MON.inflight.add(key)
        time.sleep(0.6)   # widen the window, so two copies sent together really overlap
    try:
        with MON.lock:
            result = execute(SCHEMA, doc, variable_values=variables)
            out = {'data': result.data}
            if result.errors:
                out['errors'] = [e.formatted for e in result.errors]
            out['account_id'] = 4242
            if key:
                MON.idem[key] = out
    finally:
        if key:
            with MON.lock:
                MON.inflight.discard(key)
    return 200, out, {}, detail


# ------------------------------------------------------------------ mock Twilio, Claude, Slack
OPTED_OUT = set()
SLACK_FAIL = {}   # path -> how many of the next posts the mock Slack refuses
E164 = re.compile(r'^\+[1-9]\d{7,14}$')


def twilio_api(path, headers, raw):
    def err(status, code, message):
        return status, {'code': code, 'message': message, 'more_info': f'https://www.twilio.com/docs/errors/{code}', 'status': status}, {}
    match = re.fullmatch(r'/2010-04-01/Accounts/([^/]+)/Messages\.json', path)
    if not match:
        return err(404, 20404, 'The requested resource was not found')
    auth = 'Basic ' + base64.b64encode(f'{TWILIO_SID}:{TWILIO_TOKEN}'.encode()).decode()
    if headers.get('authorization') != auth or match.group(1) != TWILIO_SID:
        return err(401, 20003, 'Authenticate')
    form = {}
    if 'application/x-www-form-urlencoded' in headers.get('content-type', ''):
        form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
    to, sender, service, body = form.get('To', ''), form.get('From', ''), form.get('MessagingServiceSid', ''), form.get('Body', '')
    if not to:
        return err(400, 21604, "A 'To' phone number is required.")
    if not E164.match(to):
        return err(400, 21211, f"Invalid 'To' Phone Number: {to}")
    if not sender and not service:
        return err(400, 21603, "A 'From' phone number is required.")
    if sender and not E164.match(sender):
        return err(400, 21212, f'Invalid From Number (caller ID): {sender}')
    if service and not re.fullmatch(r'MG[0-9a-f]{32}', service):
        return err(400, 21701, 'The Messaging Service Sid is invalid.')
    if not body:
        return err(400, 21602, 'Message body is required.')
    if len(body) > 1600:
        return err(400, 21617, 'The concatenated message body exceeds the 1600 character limit.')
    if to in OPTED_OUT:
        return err(400, 21610, 'Attempt to send to unsubscribed recipient')
    sid = 'SM' + uuid.uuid4().hex
    return 201, {'sid': sid, 'account_sid': TWILIO_SID, 'to': to, 'from': sender or None, 'messaging_service_sid': service or None, 'body': body,
                 'status': 'accepted' if service else 'queued', 'num_segments': '1', 'num_media': '0', 'direction': 'outbound-api',
                 'date_created': 'Tue, 20 Oct 2026 10:00:00 +0000', 'date_sent': None, 'date_updated': 'Tue, 20 Oct 2026 10:00:00 +0000',
                 'error_code': None, 'error_message': None, 'price': None, 'api_version': '2010-04-01',
                 'uri': f'/2010-04-01/Accounts/{TWILIO_SID}/Messages/{sid}.json'}, {}


CLAUDE = {
    'pre-approved for $480k': dict(intent='buyer', budget='$480k', timeline='asap', area='East Austin', pre_approved='yes', urgency=5,
                                   summary='Pre-approved buyer, $480k, wants a 3 bed & 2 bath in East Austin and can tour this weekend.'),
    'thinking about listing': dict(intent='seller', budget='', timeline='3_to_6_months', area='Travis Heights', pre_approved='unknown', urgency=2,
                                   summary='Owner thinking about listing a house in Travis Heights in the next few months.'),
    'rental properties': dict(intent='investor', budget='$300k each', timeline='within_3_months', area='Round Rock', pre_approved='yes', urgency=4,
                              summary='Cash investor wants two rental properties in Round Rock, about $300k each.'),
    '2 bedroom apartment': dict(intent='renter', budget='$2,000 a month', timeline='asap', area='Mueller', pre_approved='unknown', urgency=4,
                                summary='Renter needs a 2 bedroom near Mueller by the first, about $2,000 a month.'),
    'sell our condo near Zilker': 529,
    'Birch Court': 'refusal',
}
DEFAULT_CLAUDE = dict(intent='buyer', budget='', timeline='unknown', area='', pre_approved='unknown', urgency=3, summary='Buyer asking about homes.')
CLAUDE_FIELDS = {'model', 'max_tokens', 'system', 'tools', 'tool_choice', 'messages', 'output_config', 'thinking', 'metadata', 'stop_sequences',
                 'stream', 'temperature', 'top_p', 'top_k', 'service_tier'}


def claude_problems(headers, body):
    p = []
    extra = set(body) - CLAUDE_FIELDS
    if extra:
        p.append(f'{sorted(extra)[0]}: Extra inputs are not permitted')
    config = body.get('output_config')
    if config is not None:
        if 'haiku' in str(body.get('model')):
            p.append('This model does not support the effort parameter.')
        elif not isinstance(config, dict) or set(config) - {'effort', 'format'} or config.get('effort') not in ('low', 'medium', 'high', 'xhigh', 'max'):
            p.append('output_config.effort: Input should be low, medium, high, xhigh or max')
    if headers.get('x-api-key') != BASE_ENV['ANTHROPIC_API_KEY']:
        p.append('missing x-api-key')
    if headers.get('anthropic-version') != '2023-06-01':
        p.append('missing anthropic-version')
    if 'application/json' not in headers.get('content-type', ''):
        p.append('content-type must be application/json')
    if not isinstance(body.get('model'), str) or not isinstance(body.get('max_tokens'), int) or not 0 < body['max_tokens'] <= 64000:
        p.append('model and max_tokens are required')
    if not isinstance(body.get('system'), str) or not body['system']:
        p.append('system must be a string')
    msgs = body.get('messages') or []
    if len(msgs) != 1 or msgs[0].get('role') != 'user' or not isinstance(msgs[0].get('content'), str) or not msgs[0]['content']:
        p.append('expected one user message with text')
    tools = body.get('tools') or [{}]
    schema = tools[0].get('input_schema', {})
    if schema.get('type') != 'object' or not set(schema.get('required', [])) <= set(schema.get('properties', {})):
        p.append('tools.0.input_schema is invalid')
    if body.get('tool_choice') != {'type': 'tool', 'name': tools[0].get('name')}:
        p.append('tool must be defined and forced')
    if (body.get('thinking') or {}).get('type') == 'enabled':
        p.append('forced tool use is not compatible with manual extended thinking')
    return p


def claude_api(headers, raw):
    try:
        body = json.loads(raw)
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return 400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': 'The request body is not valid JSON.'}}, {}
    problems = claude_problems(headers, body)
    if problems:
        return 400, {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': '; '.join(problems)}}, {}
    content = body['messages'][0]['content']
    answer = next((v for k, v in CLAUDE.items() if k in content), DEFAULT_CLAUDE)
    if answer == 529:
        return 529, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'Overloaded'}}, {}
    if answer == 'refusal':   # a safety classifier declined: HTTP 200, no tool call
        return 200, {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'], 'content': [],
                     'stop_reason': 'refusal', 'stop_details': {'type': 'refusal', 'category': None, 'explanation': 'Declined by the mock.'},
                     'usage': {'input_tokens': 900, 'output_tokens': 0}}, {}
    return 200, {'id': 'msg_mock', 'type': 'message', 'role': 'assistant', 'model': body['model'],
                 'content': [{'type': 'tool_use', 'id': 'toolu_mock', 'name': 'classify_lead', 'input': answer}],
                 'stop_reason': 'tool_use', 'usage': {'input_tokens': 900, 'output_tokens': 90}}, {}


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, obj, extra=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        path = urllib.parse.urlparse(self.path).path
        if path == '/v2':
            status, out, extra, detail = monday_api(headers, raw)
            log(dict(service='monday', path=path, headers=headers, status=status, response=out, **detail))
        elif path.startswith('/2010-04-01/'):
            status, out, extra = twilio_api(path, headers, raw)
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode(), keep_blank_values=True).items()}
            log(dict(service='twilio', path=path, headers=headers, status=status, code=out.get('code'), form=form, to=form.get('To'),
                     body=form.get('Body', '')))
        elif path == '/v1/messages':
            status, out, extra = claude_api(headers, raw)
            try:
                request = json.loads(raw)
            except ValueError:
                request = None
            log(dict(service='claude', path=path, headers=headers, status=status, request=request))
        elif path.startswith('/slack/'):
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = None
            if 'application/json' not in headers.get('content-type', '') or not isinstance(payload, dict):
                status, out = 400, 'invalid_payload'
            elif not payload.get('text'):
                status, out = 400, 'no_text'
            elif SLACK_FAIL.get(path, 0) > 0:
                SLACK_FAIL[path] -= 1
                status, out = 404, 'channel_not_found'
            else:
                status, out = 200, 'ok'
            extra = {}
            log(dict(service='slack-errors' if path == '/slack/errors' else 'slack', path=path, status=status, text=(payload or {}).get('text', '')))
        else:
            status, out, extra = 404, {'message': 'not mocked'}, {}
        self.reply(status, out, extra)


# ------------------------------------------------------------------ n8n helpers
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(path, env, error_workflow_id=None, source=WORKFLOW):
    """A copy of the workflow with $env values replaced by test values and the API hosts pointed at the mocks."""
    wf = json.loads(source.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = path
            node['webhookId'] = str(uuid.uuid4())
    settings = dict(wf.get('settings', {}))
    if error_workflow_id:
        settings['errorWorkflow'] = error_workflow_id
    return {'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings}


def create(name, wf, activate=True):
    wid = api('POST', '/api/v1/workflows', dict(wf, name=name))['id']
    CREATED.append(wid)
    if activate:
        api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


CREATED = []


def executions(wid):
    return api('GET', f'/api/v1/executions?workflowId={wid}&limit=250').get('data', [])


def finished(wid):
    return sorted([e for e in executions(wid) if e.get('status') in ('success', 'error', 'crashed', 'canceled')], key=lambda e: int(e['id']))


def wait_finished(wid, count, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        done = finished(wid)
        if len(done) >= count:
            return done
        time.sleep(0.4)
    raise TimeoutError(f'workflow {wid}: expected {count} finished runs, got {len(finished(wid))}')


def ended(ex):
    data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
    result = data.get('resultData') or {}
    return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')


def ran(ex):
    """Names of the nodes a run executed. More reliable than the executions list, which leaves out runs n8n still tracks
    as active (a started escalation timer would not show up there while it waits)."""
    data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
    return set(((data.get('resultData') or {}).get('runData') or {}).keys())


def alert_for(ex, timeout=20):
    """The error workflow's Slack alert for this run, matched by the execution link inside it."""
    link = f"/executions/{ex['id']}"
    hit = wait_log(lambda e: e['service'] == 'slack-errors' and link in e['text'], timeout)
    return hit[0]['text'] if hit else ''


def post(path, payload, secret=SECRET, form=False, query=None):
    url = f'{N8N}/webhook/{path}' + (f'?{urllib.parse.urlencode(query)}' if query else '')
    headers = {'Content-Type': 'application/x-www-form-urlencoded' if form else 'application/json'}
    if secret:
        headers['X-Lead-Secret'] = secret
    data = urllib.parse.urlencode(payload).encode() if form else json.dumps(payload).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='POST', data=data, headers=headers), timeout=30) as r:
            return r.status, json.loads(r.read().decode() or 'null')
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or 'null')


def since(mark, service=None, **match):
    with LOG_LOCK:
        rows = LOG[mark:]
    return [e for e in rows if (service is None or e['service'] == service) and all(e.get(k) == v for k, v in match.items())]


def gql(mark, op):
    return [e for e in since(mark, 'monday') if e.get('op') == op]


def wait_log(predicate, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        with LOG_LOCK:
            hit = [e for e in LOG if predicate(e)]
        if hit:
            return hit
        time.sleep(0.3)
    return []


RESULTS = []


def check(scenario, label, ok, detail=''):
    RESULTS.append((scenario, label, bool(ok), detail))


def item_of(phone):
    ids = MON.find(phone)
    return ids[-1] if ids else None


def item_url(item_id, board=BOARD):
    return f'https://example-realty.monday.com/boards/{board}/pulses/{item_id}'


# ------------------------------------------------------------------ scenarios
PATH_A, PATH_B, PATH_C, PATH_D, PATH_E = f'{SLUG}-e2e', f'{SLUG}-e2e-quiet', f'{SLUG}-e2e-away', f'{SLUG}-e2e-roster', f'{SLUG}-e2e-nosecret'
JAMIE_MSG = "Hi! We're pre-approved for $480k and want a 3 bed & 2 bath in East Austin. Can we tour this weekend?"
JAMIE = {'firstName': 'Jamie', 'lastName': 'Rivera', 'phone': '(512) 555-0142', 'email': 'Jamie.Rivera@Example.com', 'message': JAMIE_MSG, 'source': 'Website'}
JAMIE_SUMMARY = CLAUDE['pre-approved for $480k']['summary']


def lead_run(wid, path, payload, expect_runs, mark_status=None, **kw):
    """Posts one lead, waits for the intake run (and the escalation run when expected). Returns the pieces to check."""
    base = len(finished(wid))
    mark = len(LOG)
    status, body = post(path, payload, **kw)
    runs = wait_finished(wid, base + 1)[base:]
    mid = len(LOG)
    if mark_status:
        mark_status()
    if expect_runs == 2:
        runs = wait_finished(wid, base + 2, timeout=120)[base:]
    return status, body, runs, mark, mid


def set_status_item(item_id, label):
    """An agent changes the status in monday."""
    with MON.lock:
        MON.items[item_id]['values']['lead_status'] = {'label': label}
        if label not in MON.boards[BOARD]['labels']['lead_status']:
            MON.boards[BOARD]['labels']['lead_status'].append(label)


def set_status(phone, label):
    set_status_item(item_of(phone), label)


def last_item():
    return max(MON.items, key=int)


def run_a(wid):
    # 1. Security
    forged = []
    for label, secret in (('no secret', None), ('wrong secret', 'guess-123')):
        base, mark = len(finished(wid)), len(LOG)
        status, body = post(PATH_A, JAMIE, secret=secret)
        run = wait_finished(wid, base + 1)[-1]
        forged.append(run)
        node, _ = ended(run)
        check(f'Forged request, {label}', 'refused with 401, nothing called, run flagged',
              status == 401 and body == {'ok': False, 'error': 'unauthorized'} and not [e for e in since(mark) if e['service'] != 'slack-errors']
              and node == 'Stop: Bad Secret', f'{status}, ended at {node}')
    alerts = [alert_for(run) for run in forged]
    check('Forged request, no secret / wrong secret', 'the error workflow posts one Slack alert per refused run, with its link',
          all('failed at *Stop: Bad Secret*' in a and 'X-Lead-Secret' in a for a in alerts),
          alerts[0].splitlines()[0] if alerts[0] else 'no alert')

    # 2. Nothing to contact
    base, mark = len(finished(wid)), len(LOG)
    status, body = post(PATH_A, {'name': 'Bot', 'phone': '12', 'email': 'not-an-email', 'message': 'hello'})
    node, _ = ended(wait_finished(wid, base + 1)[-1])
    check('No valid phone or email', 'refused with 400, monday never called',
          status == 400 and body == {'ok': False, 'error': 'a valid phone number or email is required'} and not since(mark) and node == 'Respond: 400',
          f'{status} {body}, ended at {node}')

    # 3. Happy path: website form, JSON
    status, body, runs, mark, mid = lead_run(wid, PATH_A, JAMIE, 2)
    intake, esc = runs
    item = item_of('+15125550142')
    url = item_url(item)
    check('New lead (website form)', 'answered 202 right away', status == 202 and body == {'ok': True, 'status': 'accepted'}, f'{status} {body}')
    find = gql(mark, 'FindLead')
    check('New lead (website form)', 'duplicate search by phone digits and lowercased email, with token and API version',
          len(find) == 1 and find[0]['variables']['phone'] == '15125550142' and find[0]['variables']['email'] == 'jamie.rivera@example.com'
          and find[0]['headers'].get('authorization') == MONDAY_TOKEN and find[0]['headers'].get('api-version') == MONDAY_VERSION
          and find[0]['roots'] == ['items_page_by_column_values', 'items_page_by_column_values'], json.dumps(find[0]['variables']) if find else 'no search')
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    expected_cols = {'lead_phone': {'phone': '+15125550142', 'countryShortName': 'US'},
                     'lead_email': {'email': 'jamie.rivera@example.com', 'text': 'jamie.rivera@example.com'},
                     'lead_status': {'label': 'New'}, 'lead_agent': 'Maya Chen', 'lead_source': 'Website',
                     'lead_message': {'text': JAMIE_MSG}, 'lead_run': str(intake['id'])}
    check('New lead (website form)', 'one monday item with phone (E.164 + country), email, status New, agent, source, message, run ID',
          len(created) == 1 and cols == expected_cols and created[0]['variables']['name'] == 'Jamie Rivera'
          and created[0]['variables']['group'] == 'new_leads' and re.fullmatch(r'[0-9a-f]{64}', created[0]['headers'].get('idempotency-key', '')),
          json.dumps(cols)[:160])
    texts = since(mark, 'twilio')
    lead_text = [e for e in texts if e['to'] == '+15125550142']
    expected_lead_text = ('Hi Jamie, thanks for reaching out to Acme Realty! Maya Chen will call or text you in the next few minutes. '
                          'You can also reach Maya directly at (512) 555-0101. Reply STOP to opt out.')
    check('New lead (website form)', 'instant text to the lead names the agent (basic auth, form encoded, From number)',
          len(lead_text) == 1 and lead_text[0]['status'] == 201 and lead_text[0]['body'] == expected_lead_text
          and lead_text[0]['form'].get('From') == TWILIO_FROM, lead_text[0]['body'] if lead_text else 'no text')
    claude = since(mark, 'claude')
    order = [e['n'] for e in since(mark) if e['service'] in ('claude', 'twilio')]
    check('New lead (website form)', 'the lead is texted before Claude is even called (speed first)',
          lead_text and claude and lead_text[0]['n'] < claude[0]['n'], f'log order {order}')
    content = claude[0]['request']['messages'][0]['content'] if claude else ''
    check('New lead (website form)', 'one Claude call, forced tool, low effort; it gets the message but not the name, phone or email',
          len(claude) == 1 and claude[0]['status'] == 200 and claude[0]['request']['model'] == 'claude-sonnet-5' and JAMIE_MSG in content
          and claude[0]['request']['tool_choice'] == {'type': 'tool', 'name': 'classify_lead'} and claude[0]['request']['max_tokens'] == 2048
          and claude[0]['request'].get('output_config') == {'effort': 'low'}
          and '555' not in content and 'example.com' not in content.lower() and 'Rivera' not in content, f'{len(claude)} call(s)')
    saved = gql(mark, 'SaveDetails')
    expected_details = {'lead_intent': 'Buyer', 'lead_budget': '$480k', 'lead_timeline': 'ASAP', 'lead_area': 'East Austin',
                        'lead_preapproved': 'Yes', 'lead_urgency': '5', 'lead_summary': JAMIE_SUMMARY, 'lead_classified_by': 'Claude'}
    check('New lead (website form)', "Claude's details saved on the item", len(saved) == 1 and json.loads(saved[0]['variables']['cols']) == expected_details
          and saved[0]['variables']['item'] == item and saved[0]['response'].get('data'), saved[0]['variables']['cols'][:120] if saved else 'not saved')
    agent_text = [e for e in texts if e['to'] == MAYA]
    expected_agent = '\n'.join(['New lead for you: Jamie Rivera', 'Buyer, urgency 5/5', 'Budget: $480k', 'Timeline: ASAP', 'Area: East Austin',
                                'Pre-approved: Yes', f'"{JAMIE_SUMMARY}"', 'Phone: (512) 555-0142', 'Email: jamie.rivera@example.com',
                                'Source: Website', f'Open in monday: {url}',
                                f'Update the status in monday within {MINUTES} min, or the lead goes to the next agent.'])
    check('New lead (website form)', 'agent text with the summary and the monday link', len(agent_text) == 1 and agent_text[0]['body'] == expected_agent,
          agent_text[0]['body'].splitlines()[0] if agent_text else 'no text')
    node, _ = ended(intake)
    check('New lead (website form)', 'run ends cleanly, no flags', intake['status'] == 'success' and node == 'Needs Attention?', f'ended at {node}')

    # Escalation of the same lead: nobody changed the status
    read = gql(mid, 'ReadLead')
    reassign = gql(mid, 'Reassign')
    lead_created_at = created[0]['t'] if created else 0
    check('Escalation, status still New', f'status read again {MINUTES * 60:.0f} seconds later, as its own run',
          len(read) == 1 and read[0]['variables'] == {'item': [item], 'cols': ['lead_status', 'lead_agent']} and esc['mode'] == 'integrated'
          and read[0]['t'] - lead_created_at >= MINUTES * 60 - 1, f"after {read[0]['t'] - lead_created_at:.1f} s" if read else 'no read')
    check('Escalation, status still New', 'reassigned to Leo Park (Ana Diaz is away) with a note on the item',
          len(reassign) == 1 and json.loads(reassign[0]['variables']['cols']) == {'lead_agent': 'Leo Park'}
          and reassign[0]['variables']['note'] == f'Still New after {MINUTES} minutes, so this lead moved from Maya Chen to Leo Park.'
          and MON.items[item]['values']['lead_agent'] == 'Leo Park', reassign[0]['variables']['note'] if reassign else 'no reassign')
    leo = [e for e in since(mid, 'twilio') if e['to'] == LEO]
    check('Escalation, status still New', 'Leo gets a text with the lead and the link',
          len(leo) == 1 and leo[0]['body'].startswith('Lead reassigned to you: Jamie Rivera\nBuyer, urgency 5/5') and url in leo[0]['body']
          and 'Maya Chen did not update it in time' in leo[0]['body'], leo[0]['body'].splitlines()[0] if leo else 'no text')
    slack = since(mid, 'slack')
    expected_slack = '\n'.join([f'<@{BROKER}> :warning: *Lead not picked up within {MINUTES} minutes*', 'Jamie Rivera: Buyer, urgency 5/5, from Website',
                                f"> {JAMIE_SUMMARY.replace('&', '&amp;')}", f"Moved from Maya Chen <@{SLACK_IDS['Maya Chen']}> to Leo Park <@{SLACK_IDS['Leo Park']}>, who was just texted.",
                                f'Open in monday: {url}'])
    node, _ = ended(esc)
    check('Escalation, status still New', 'broker alerted in Slack with both agents mentioned',
          len(slack) == 1 and slack[0]['text'] == expected_slack and node == 'Slack: Alert Broker', slack[0]['text'].splitlines()[0] if slack else 'no alert')

    # 4. Portal JSON (nested), agent changes the status in time
    sam = {'source': 'Example Listings', 'lead': {'contact': {'firstName': 'sam', 'lastName': 'Cole', 'phone': '512.555.0155', 'email': 'sam.cole@example.com'},
                                       'message': "We're thinking about listing our house in Travis Heights in the next few months."},
           'listing': {'address': '1200 Example Ave, Austin, TX', 'price': 650000}}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, sam, 2, mark_status=lambda: set_status('+15125550155', 'Contacted'))
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    sam_text = [e for e in since(mark, 'twilio') if e['to'] == '+15125550155']
    check('Portal lead (nested JSON)', 'fields read from the nested payload, phone with dots normalized, next agent in turn (Leo)',
          cols.get('lead_phone') == {'phone': '+15125550155', 'countryShortName': 'US'} and cols.get('lead_agent') == 'Leo Park'
          and cols.get('lead_source') == 'Example Listings' and created[0]['variables']['name'] == 'sam Cole'
          and cols.get('lead_message', {}).get('text', '').startswith('Property: 1200 Example Ave, Austin, TX')
          and sam_text and sam_text[0]['body'].startswith('Hi Sam, thanks for reaching out to Acme Realty! Leo Park will'),
          json.dumps({k: cols.get(k) for k in ('lead_agent', 'lead_source')}))
    node, _ = ended(runs[1])
    check('Agent updates the status in time', 'escalation run only reads the status, then stops',
          len(gql(mid, 'ReadLead')) == 1 and not gql(mid, 'Reassign') and not since(mid, 'twilio') and not since(mid, 'slack') and node == 'Still New?',
          f'ended at {node}')

    # 5. Same lead again within DEDUPE_HOURS
    again = dict(JAMIE, email='JAMIE.RIVERA@example.com', message='Also, can we see the one on Elm Street?', source='Open House')
    status, body, runs, mark, mid = lead_run(wid, PATH_A, again, 1)
    time.sleep(2)
    upd = gql(mark, 'UpdateLead')
    node, _ = ended(runs[0])
    check('Same lead within DEDUPE_HOURS', 'existing item gets a note, nothing new is created, nobody is texted, no Claude call',
          status == 202 and len(upd) == 1 and upd[0]['roots'] == ['create_update'] and upd[0]['variables']['item'] == item
          and 'came in again' in upd[0]['variables']['note'] and 'via Open House' in upd[0]['variables']['note']
          and 'New message: Also, can we see the one on Elm Street?' in upd[0]['variables']['note']
          and not gql(mark, 'CreateLead') and not since(mark, 'twilio') and not since(mark, 'claude') and node == 'Update Saved?',
          f'ended at {node}')
    executed = ran(runs[0])
    check('Same lead within DEDUPE_HOURS', 'no escalation timer started, no item created, no texts',
          'Start Escalation Timer' not in executed and 'Monday: Create Item' not in executed and not executed & {'Twilio: Text Lead', 'Twilio: Alert Agent'},
          f'{len(executed)} nodes ran')

    # 6. Form post (urlencoded), email only, secret in the query string; then the agent is changed by hand
    chris = {'name': 'Chris Lane', 'email': 'chris.lane@example.com', 'message': 'Looking for two rental properties in Round Rock, cash, about $300k each.'}

    def move_chris():
        with MON.lock:
            MON.items[last_item()]['values']['lead_agent'] = 'Ana Diaz'
    status, body, runs, mark, mid = lead_run(wid, PATH_A, chris, 2, mark_status=move_chris, secret=None, form=True, query={'key': SECRET})
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    agent_text = [e for e in since(mark, 'twilio') if e['to'] == MAYA]
    check('Email only lead (form post, ?key=)', 'accepted, routed to Maya, no text to the lead, the agent is told it is email only',
          status == 202 and 'lead_phone' not in cols and cols.get('lead_agent') == 'Maya Chen' and len(since(mark, 'twilio')) == 1
          and agent_text and 'No valid phone number, email only.' in agent_text[0]['body'] and 'Investor, urgency 4/5' in agent_text[0]['body'],
          agent_text[0]['body'].splitlines()[1] if agent_text else 'no agent text')
    node, _ = ended(runs[1])
    check('Agent changed by hand', 'escalation sees another agent on the item and does nothing',
          not gql(mid, 'Reassign') and not since(mid, 'twilio') and not since(mid, 'slack') and node == 'Still New?', f'ended at {node}')

    # 7. Chris comes back with a phone number: duplicate by email, phone is filled in
    chris_item = last_item()
    status, body, runs, mark, mid = lead_run(wid, PATH_A, dict(chris, phone='+1 512 555 0166', message='Here is my cell.'), 1, secret=None, form=True,
                                             query={'key': SECRET})
    upd = gql(mark, 'UpdateLead')
    check('Same lead by email, now with a phone', 'the phone is added to the existing item, no new routing',
          len(upd) == 1 and upd[0]['roots'] == ['change_multiple_column_values', 'create_update']
          and json.loads(upd[0]['variables']['cols']) == {'lead_phone': {'phone': '+15125550166', 'countryShortName': 'US'}}
          and MON.items[chris_item]['values'].get('lead_phone') == {'phone': '+15125550166', 'countryShortName': 'US'}
          and not since(mark, 'twilio') and not gql(mark, 'CreateLead'), upd[0]['variables'].get('cols', '') if upd else 'no update')

    # 8. The same submission delivered twice at the same moment
    dana = {'name': 'Dana Fox', 'phone': '5125550177', 'email': 'dana.fox@example.com', 'message': 'Need a 2 bedroom apartment near Mueller by the first.',
            'source': 'Website'}
    base, mark = len(finished(wid)), len(LOG)
    threads = [threading.Thread(target=post, args=(PATH_A, dana)) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    intakes = wait_finished(wid, base + 2)[base:]
    set_status('+15125550177', 'Contacted')
    runs = wait_finished(wid, base + 3, timeout=120)[base:]
    creates = gql(mark, 'CreateLead')
    keys = {e['headers'].get('idempotency-key') for e in creates}
    ends = sorted(ended(r)[0] for r in runs if r['mode'] == 'webhook')
    dana_texts = [e for e in since(mark, 'twilio') if e['to'] == '+15125550177']
    agent_texts = [e for e in since(mark, 'twilio') if e['to'] in (MAYA, LEO)]
    check('Same submission twice at once', 'exactly one monday item, one text to the lead, one to the agent',
          len(MON.find('+15125550177')) == 1 and len(dana_texts) == 1 and len(agent_texts) == 1, f'{len(MON.find("+15125550177"))} item(s), {len(dana_texts)} lead text(s)')
    check('Same submission twice at once', "both create calls carried the same Idempotency-Key; the second got monday's 409 or replay and stopped",
          len(keys) == 1 and len(creates) >= 2 and any(e.get('conflict') or e.get('replayed') for e in creates)
          and ends == ['Copy Confirmed?', 'Needs Attention?'] and len([r for r in runs if r['mode'] == 'integrated']) == 1,
          f'{len(creates)} create calls, runs ended at {ends}')

    # 9. monday search down while the same lead is sent again: the idempotency key still stops a double text
    MON.fail_500['items_page_by_column_values'] = 3
    status, body, runs, mark, mid = lead_run(wid, PATH_A, dana, 1)
    time.sleep(1)
    node, _ = ended(runs[0])
    replay = gql(mark, 'CreateLead')
    check('Search down, same lead sent again', 'search retried 3 times, then the create call is replayed by monday and the run stops, no texts',
          len(gql(mark, 'FindLead')) == 3 and replay and replay[0].get('replayed') and not since(mark, 'twilio') and len(MON.find('+15125550177')) == 1
          and node == 'Copy Confirmed?' and runs[0]['status'] == 'success', f'ended at {node}')

    # 10. Claude down: keyword rules fill in, the lead is still routed, the run is flagged
    erin = {'name': 'Erin Wolfe', 'phone': '512-555-0188', 'message': 'We want to sell our condo near Zilker this spring.'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, erin, 2, mark_status=lambda: set_status('+15125550188', 'Contacted'))
    saved = gql(mark, 'SaveDetails')
    details = json.loads(saved[0]['variables']['cols']) if saved else {}
    node, error = ended(runs[0])
    erin_texts = [e for e in since(mark, 'twilio') if e['to'] == '+15125550188']
    agent_text = [e for e in since(mark, 'twilio') if e['to'] == MAYA]
    check('Claude down (529)', 'retried 3 times, then sorted by keyword rules (seller, 3 to 6 months) and marked as such in monday',
          len(since(mark, 'claude')) == 3 and details.get('lead_intent') == 'Seller' and details.get('lead_timeline') == '3 to 6 months'
          and details.get('lead_classified_by') == 'Rules (Claude unavailable)' and 'keyword rules' in saved[0]['variables'].get('note', ''),
          json.dumps({k: details.get(k) for k in ('lead_intent', 'lead_timeline', 'lead_urgency')}))
    check('Claude down (529)', 'the lead and the agent are still texted, the agent sees the AI summary is missing',
          len(erin_texts) == 1 and agent_text and '(AI summary unavailable)' in agent_text[0]['body'], f'{len(erin_texts)} lead text(s)')
    alert = alert_for(runs[0])
    check('Claude down (529)', 'run flagged at Stop: Needs Attention and posted to Slack by the error workflow',
          node == 'Stop: Needs Attention' and 'Claude did not answer after 3 tries' in error and 'overloaded' in error.lower()
          and 'Claude did not answer after 3 tries' in alert and 'Erin Wolfe' in alert,
          error.splitlines()[1][:90] if '\n' in error else error[:90])

    # 11. The lead has opted out of texts (Twilio 21610)
    OPTED_OUT.add('+15125550199')
    gale = {'name': 'Gale Moss', 'phone': '+15125550199', 'email': 'gale@example.com', 'message': 'Is the house on Oak Lane still available?'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, gale, 2, mark_status=lambda: set_status('+15125550199', 'Contacted'))
    node, error = ended(runs[0])
    tries = [e for e in since(mark, 'twilio') if e['to'] == '+15125550199']
    agent_text = [e for e in since(mark, 'twilio') if e['to'] == LEO]
    saved = gql(mark, 'SaveDetails')
    check('Lead text refused (opted out)', 'retried 3 times; the agent is told to call; a note goes on the monday item',
          len(tries) == 3 and all(e['status'] == 400 for e in tries) and agent_text and 'did NOT go through' in agent_text[0]['body']
          and saved and 'did not go through' in saved[0]['variables'].get('note', '') and '21610' in saved[0]['variables'].get('note', ''),
          saved[0]['variables'].get('note', '')[:100] if saved else 'no save')
    check('Lead text refused (opted out)', 'run flagged with the Twilio reason', node == 'Stop: Needs Attention' and '21610' in error, error.splitlines()[1][:90] if '\n' in error else error)

    # 12. monday down when the item is created
    MON.fail_500['create_item'] = 3
    harper = {'name': 'Harper Quinn', 'phone': '+15125550123', 'message': 'Looking for a first home under $350k.'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, harper, 1)
    time.sleep(1)
    node, error = ended(runs[0])
    texts = since(mark, 'twilio')
    agent_text = [e for e in texts if e['to'] == MAYA]
    check('monday down when saving', 'create retried 3 times; the lead and the agent are still texted; no details save, no timer',
          len(gql(mark, 'CreateLead')) == 3 and len([e for e in texts if e['to'] == '+15125550123']) == 1 and len(agent_text) == 1
          and 'NOT in monday' in agent_text[0]['body'] and not gql(mark, 'SaveDetails')
          and 'Start Escalation Timer' not in ran(runs[0]), f'{len(texts)} texts')
    check('monday down when saving', 'run flagged with the lead details for a manual add',
          node == 'Stop: Needs Attention' and 'NOT saved in monday' in error and '+15125550123' in error, error.splitlines()[1][:90] if '\n' in error else error)

    # 13. A Canadian number (Toronto) at a US office, and monday answers 200 with errors when the AI details are saved
    MON.fail_gql['change_multiple_column_values'] = 1
    indy = {'name': 'Indy Shaw', 'phone': '(416) 555-0133', 'message': 'Can I see a townhouse near Domain?'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, indy, 2, mark_status=lambda: set_status('+14165550133', 'Contacted'))
    node, error = ended(runs[0])
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    check('Canadian number at a US office', "saved with country CA, which monday's number check accepts (a US tag would be refused)",
          cols.get('lead_phone') == {'phone': '+14165550133', 'countryShortName': 'CA'} and created[0]['response'].get('data', {}).get('create_item')
          and len([e for e in since(mark, 'twilio') if e['to'] == '+14165550133']) == 1, json.dumps(cols.get('lead_phone')))
    check('monday returns 200 with errors', 'the error is not mistaken for success: run flagged with the monday error code',
          node == 'Stop: Needs Attention' and 'monday did not save the AI details' in error and 'ColumnValueException' in error,
          error.splitlines()[1][:100] if '\n' in error else error)

    # 14. Escalation cannot read the status
    jordan = {'name': 'Jordan Pike', 'phone': '+15125550144', 'message': 'What is the HOA fee at the Pine Street condos?'}

    def break_read():
        MON.fail_500['items'] = 3
    status, body, runs, mark, mid = lead_run(wid, PATH_A, jordan, 2, mark_status=break_read)
    node, error = ended(runs[1])
    alert = alert_for(runs[1])
    check('Escalation cannot read monday', 'read retried 3 times, nothing reassigned, run flagged with the item link and posted to Slack',
          len(gql(mid, 'ReadLead')) == 3 and not gql(mid, 'Reassign') and not since(mid, 'twilio') and node == 'Stop: Status Check Failed'
          and 'NOT escalated' in error and item_url(item_of('+15125550144')) in error and 'Jordan Pike' in alert, f'ended at {node}')

    # 15. monday fails while a repeat submission is noted on the existing item
    MON.fail_500['create_update'] = 3
    base, mark = len(finished(wid)), len(LOG)
    status, body = post(PATH_A, dict(JAMIE, message='Any update on the tour?'))
    run = wait_finished(wid, base + 1)[-1]
    time.sleep(1)
    node, error = ended(run)
    alert = alert_for(run)
    check('Repeat submission, monday down', 'note retried 3 times, nothing routed or texted, run flagged with the item link',
          status == 202 and len(gql(mark, 'UpdateLead')) == 3 and not gql(mark, 'CreateLead') and not since(mark, 'twilio')
          and node == 'Stop: Update Failed' and item_url(item) in error and 'failed at *Stop: Update Failed*' in alert, f'ended at {node}')


def run_a2(wid):
    """More failure paths on the main copy. The round robin carries on from run_a: the next lead goes to Leo."""
    # 16. A phone number that cannot be read, but a valid email
    pat = {'name': 'Pat Lowe', 'phone': '555-01', 'email': 'pat.lowe@example.com', 'message': 'Looking to buy a condo downtown next month.'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, pat, 2, mark_status=lambda: set_status_item(last_item(), 'Contacted'))
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    texts = since(mark, 'twilio')
    node, error = ended(runs[0])
    check('Unreadable phone, valid email', 'routed by email to Leo; nobody texts the lead; the agent is told it is email only',
          status == 202 and 'lead_phone' not in cols and cols.get('lead_email') == {'email': 'pat.lowe@example.com', 'text': 'pat.lowe@example.com'}
          and cols.get('lead_agent') == 'Leo Park' and len(texts) == 1 and texts[0]['to'] == LEO
          and 'No valid phone number, email only.' in texts[0]['body'], json.dumps({k: cols.get(k) for k in ('lead_agent', 'lead_email')})[:120])
    check('Unreadable phone, valid email', 'run flagged with the number that could not be read',
          node == 'Stop: Needs Attention' and 'The phone number "555-01" could not be read' in error and 'Pat Lowe' in alert_for(runs[0]),
          error.splitlines()[1][:90] if '\n' in error else error)

    # 17. Claude declines (stop_reason refusal, no tool call)
    quinn = {'name': 'Quinn Hale', 'phone': '+15125550145', 'message': 'What is the asking price on Birch Court?'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, quinn, 2, mark_status=lambda: set_status('+15125550145', 'Contacted'))
    claude = since(mark, 'claude')
    saved = gql(mark, 'SaveDetails')
    details = json.loads(saved[0]['variables']['cols']) if saved else {}
    texts = since(mark, 'twilio')
    node, error = ended(runs[0])
    check('Claude answers without the tool (refusal)', 'one call, not retried; keyword rules fill in and the item says so',
          len(claude) == 1 and claude[0]['status'] == 200 and details.get('lead_classified_by') == 'Rules (Claude unavailable)'
          and 'keyword rules' in saved[0]['variables'].get('note', ''), json.dumps({k: details.get(k) for k in ('lead_intent', 'lead_classified_by')}))
    check('Claude answers without the tool (refusal)', 'the lead and Maya are still texted; the run is flagged with the stop reason',
          [e['to'] for e in texts] == ['+15125550145', MAYA] and node == 'Stop: Needs Attention' and 'stop_reason refusal' in error,
          error.splitlines()[1][:90] if '\n' in error else error)

    # 18. monday is still processing the same Idempotency-Key on every try (409 three times)
    MON.fail_409['create_item'] = 3
    uma = {'name': 'Uma Grant', 'phone': '+15125550148', 'email': 'uma.grant@example.com', 'message': 'Do you list homes in Pflugerville?'}
    status, body, runs, mark, mid = lead_run(wid, PATH_A, uma, 1)
    time.sleep(1)
    node, error = ended(runs[0])
    creates = gql(mark, 'CreateLead')
    executed = ran(runs[0])
    check('monday busy with the same key (409 on every try)', 'create tried 3 times with one key; no texts, no Claude call, no timer',
          status == 202 and len(creates) == 3 and all(e['status'] == 409 for e in creates)
          and len({e['headers'].get('idempotency-key') for e in creates}) == 1 and not since(mark, 'twilio') and not since(mark, 'claude')
          and 'Start Escalation Timer' not in executed, f'{len(creates)} create calls, ended at {node}')
    alert = alert_for(runs[0])
    check('monday busy with the same key (409 on every try)', 'the run does not end quietly: Stop: Not Routed names the lead and the error workflow posts it',
          node == 'Stop: Not Routed' and 'Uma Grant, +15125550148, uma.grant@example.com was NOT routed' in error and 'IDEMPOTENCY_CONFLICT' in error
          and 'failed at *Stop: Not Routed*' in alert, error[:100])

    # 19. The text to the agent is refused (Leo has opted out)
    OPTED_OUT.add(LEO)
    rory = {'name': 'Rory Banks', 'phone': '+15125550146', 'message': 'Can we tour the townhome on Cedar Street on Saturday?'}

    def after_rory():
        OPTED_OUT.discard(LEO)
        set_status('+15125550146', 'Contacted')
    status, body, runs, mark, mid = lead_run(wid, PATH_A, rory, 2, mark_status=after_rory)
    texts = since(mark, 'twilio')
    node, error = ended(runs[0])
    lead_texts = [e for e in texts if e['to'] == '+15125550146']
    leo_tries = [e for e in texts if e['to'] == LEO]
    check('Agent text refused', 'the lead is texted once; the text to Leo is tried 3 times',
          len(lead_texts) == 1 and lead_texts[0]['status'] == 201 and len(leo_tries) == 3 and all(e['code'] == 21610 for e in leo_tries),
          f'{len(leo_tries)} tries to Leo')
    check('Agent text refused', 'run flagged so the broker can pass the lead on, posted to Slack',
          node == 'Stop: Needs Attention' and 'The text to Leo Park failed' in error and '21610' in error and 'Rory Banks' in alert_for(runs[0]),
          error.splitlines()[1][:90] if '\n' in error else error)

    # 20. Escalation: the reassignment fails in monday. The lead's source has a Slack control sequence in it.
    sky = {'name': 'Sky Reed', 'phone': '+15125550147', 'message': 'Is the bungalow on Maple Row open for a showing?', 'source': '<!channel> Social Ad'}

    def break_reassign():
        MON.fail_500['change_multiple_column_values'] = 3
    status, body, runs, mark, mid = lead_run(wid, PATH_A, sky, 2, mark_status=break_reassign)
    sky_item = item_of('+15125550147')
    reassign = gql(mid, 'Reassign')
    slack = since(mid, 'slack')
    node, _ = ended(runs[1])
    expected = '\n'.join([f'<@{BROKER}> :warning: *Lead not picked up within {MINUTES} minutes*',
                          'Sky Reed: Buyer, urgency 3/5, from &lt;!channel&gt; Social Ad', '> Buyer asking about homes.',
                          'Could NOT reassign it to Leo Park in monday (500 Internal server error). It is still with Maya Chen. Please reassign it by hand.',
                          f'Open in monday: {item_url(sky_item)}'])
    check('Reassignment fails', 'reassign tried 3 times; the lead stays with Maya; Leo is not texted',
          len(reassign) == 3 and all(e['status'] == 500 for e in reassign) and MON.items[sky_item]['values']['lead_agent'] == 'Maya Chen'
          and not since(mid, 'twilio'), f'{len(reassign)} reassign calls')
    check('Reassignment fails', 'the broker is asked to move it by hand; what the lead typed is escaped, so <!channel> pings nobody',
          len(slack) == 1 and slack[0]['text'] == expected and '<!channel>' not in slack[0]['text'] and node == 'Slack: Alert Broker',
          slack[0]['text'].splitlines()[1] if slack else 'no alert')

    # 21. Escalation: the next agent's text fails and Slack refuses the broker alert
    tess = {'name': 'Tess Ford', 'phone': '+15125550149', 'message': 'Do you have any lake houses near Lakeway?'}

    def break_next():
        OPTED_OUT.add(MAYA)
        SLACK_FAIL['/slack/hook'] = 3
    status, body, runs, mark, mid = lead_run(wid, PATH_A, tess, 2, mark_status=break_next)
    OPTED_OUT.discard(MAYA)
    esc = runs[1]
    node, _ = ended(esc)
    tess_item = item_of('+15125550149')
    maya_tries = [e for e in since(mid, 'twilio') if e['to'] == MAYA]
    slack = since(mid, 'slack')
    outcome = (f"Moved from Leo Park <@{SLACK_IDS['Leo Park']}> to Maya Chen <@{SLACK_IDS['Maya Chen']}>, but the text to Maya Chen did NOT go through "
               '(400 21610 Attempt to send to unsubscribed recipient). Please call them.')
    check('Next agent text and Slack both fail', 'reassigned to Maya in monday; her text is tried 3 times',
          MON.items[tess_item]['values']['lead_agent'] == 'Maya Chen' and len(gql(mid, 'Reassign')) == 1 and len(maya_tries) == 3
          and all(e['code'] == 21610 for e in maya_tries), f'{len(maya_tries)} tries to Maya')
    alert = alert_for(esc)
    check('Next agent text and Slack both fail', 'the broker alert says the text failed; Slack refused it 3 times, so the run fails and the error workflow reports it',
          len(slack) == 3 and all(e['status'] == 404 and outcome in e['text'] for e in slack) and esc['status'] == 'error'
          and node == 'Slack: Alert Broker' and 'failed at *Slack: Alert Broker*' in alert, f"{len(slack)} Slack tries, run {esc['status']}")

    # Round robin over everything the main copy routed
    first = {}
    for e in gql(0, 'CreateLead'):
        first.setdefault(e['variables']['name'], json.loads(e['variables']['cols'])['lead_agent'])
    first.pop('Uma Grant', None)   # never routed, so it must not move the pointer either
    routed = list(first.values())
    check('Round robin', 'leads alternate Maya, Leo, Maya...; Ana (away) never gets one; repeats and unrouted leads do not move the pointer',
          len(routed) == 14 and routed == ['Maya Chen', 'Leo Park'] * 7, ', '.join(a.split()[0] for a in routed))


def run_b(wid, quiet_end_utc):
    kim = {'name': 'Kim Hart', 'phone': '+15125550111', 'message': 'Is the condo on 5th Street still for sale?'}
    base, mark = len(finished(wid)), len(LOG)
    status, body = post(PATH_B, kim)
    intake = wait_finished(wid, base + 1)[-1]
    texts = since(mark, 'twilio')
    lead_text = [e for e in texts if e['to'] == '+15125550111']
    agent_text = [e for e in texts if e['to'] == MAYA]
    check('Quiet hours', 'the lead is texted at once, but promised a morning follow up',
          lead_text and 'Maya Chen will get back to you first thing in the morning' in lead_text[0]['body'], lead_text[0]['body'][:80] if lead_text else 'no text')
    expected = quiet_end_utc + timedelta(seconds=6)
    local = (expected - timedelta(hours=5)).strftime('%I:%M %p').lstrip('0')
    check('Quiet hours', 'the agent is told the deadline in local time', agent_text and f'by {local}' in agent_text[0]['body'],
          agent_text[0]['body'].splitlines()[-1] if agent_text else 'no text')
    # The intake run records the ID of the escalation run it started. n8n's list endpoint leaves out runs it still
    # tracks as active, so the parked run is fetched by that ID.
    timer = api('GET', f"/api/v1/executions/{intake['id']}?includeData=true")['data']['resultData']['runData']['Start Escalation Timer'][0]
    sub = timer['data']['main'][0][0]['metadata']['subExecution']['executionId']
    parked = api('GET', f'/api/v1/executions/{sub}')
    wait_till = datetime.fromisoformat(parked['waitTill'].replace('Z', '+00:00')) if parked.get('waitTill') else None
    check('Quiet hours', 'the escalation run is parked in the database until quiet hours end plus ESCALATION_MINUTES',
          parked.get('status') == 'waiting' and parked.get('mode') == 'integrated' and wait_till and abs((wait_till - expected).total_seconds()) < 2,
          f"{parked.get('status')}, waitTill {wait_till:%H:%M:%S} UTC, expected {expected:%H:%M:%S}" if wait_till else f"{parked.get('status')}, no waitTill")
    seconds = max(0, (expected - datetime.now(timezone.utc)).total_seconds())
    print(f'  (waiting {seconds:.0f} s for the escalation run that n8n parked in the database)')
    mid = len(LOG)
    runs = wait_finished(wid, base + 2, timeout=seconds + 120)[base:]
    node, _ = ended(runs[1])
    slack = since(mid, 'slack')
    check('Quiet hours, long wait', 'n8n resumes the parked run; no other agent is free, so the broker is asked to follow up',
          node == 'Slack: Alert Broker' and slack and 'after quiet hours' in slack[0]['text']
          and f"No other agent is available, so it is still with Maya Chen <@{SLACK_IDS['Maya Chen']}>" in slack[0]['text'] and not gql(mid, 'Reassign'),
          slack[0]['text'].splitlines()[0] if slack else f'ended at {node}')


def run_c(wid):
    lee = {'name': 'Lee Park', 'phone': '07700 900123', 'email': 'lee@example.com', 'message': 'Relocating from London, need a family home.'}
    status, body, runs, mark, mid = lead_run(wid, PATH_C, lee, 1)
    time.sleep(1)
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    texts = since(mark, 'twilio')
    node, error = ended(runs[0])
    check('Everyone away (custom columns, UK number)', 'the lead gets a text from the office; the item is Unassigned in the mapped columns',
          cols.get('status') == {'label': 'New'} and cols.get('text_agent') == 'Unassigned' and 'lead_status' not in cols
          and cols.get('lead_phone') == {'phone': '+447700900123', 'countryShortName': 'GB'} and len(texts) == 1 and texts[0]['to'] == '+447700900123'
          and 'One of our agents will get back to you' in texts[0]['body'], json.dumps({k: cols.get(k) for k in ('status', 'text_agent', 'lead_phone')}))
    check('Everyone away (custom columns, UK number)', 'texts go through the Messaging Service; nobody else texted; no timer; run flagged',
          texts and texts[0]['form'].get('MessagingServiceSid') == MESSAGING_SERVICE and 'From' not in texts[0]['form']
          and node == 'Stop: Needs Attention' and 'No agent is available' in error and len(finished(wid)) == 1
          and not ran(runs[0]) & {'Start Escalation Timer', 'Twilio: Alert Agent'},
          error.splitlines()[1][:80] if '\n' in error else error)


def run_d(wid):
    mo = {'name': 'Mo Reyes', 'phone': '+15125550122', 'message': 'Do you have any homes with a pool in Cedar Park?'}
    status, body, runs, mark, mid = lead_run(wid, PATH_D, mo, 1)
    time.sleep(1)
    created = gql(mark, 'CreateLead')
    cols = json.loads(created[0]['variables']['cols']) if created else {}
    texts = since(mark, 'twilio')
    node, error = ended(runs[0])
    check('AGENTS_JSON is broken', 'the lead still gets the office text and an Unassigned item; the run says AGENTS_JSON is the problem',
          status == 202 and cols.get('lead_agent') == 'Unassigned' and len(texts) == 1 and texts[0]['to'] == '+15125550122'
          and 'One of our agents will get back to you' in texts[0]['body'] and node == 'Stop: Needs Attention'
          and 'AGENTS_JSON is empty or is not valid JSON' in error and len(finished(wid)) == 1
          and not ran(runs[0]) & {'Start Escalation Timer', 'Twilio: Alert Agent'},
          error.splitlines()[1][:80] if '\n' in error else error)


def run_e(wid):
    """LEAD_WEBHOOK_SECRET left empty: the workflow must refuse everything, even a request whose secret is also empty."""
    base, mark = len(finished(wid)), len(LOG)
    first = post(PATH_E, JAMIE, secret=None)
    second = post(PATH_E, JAMIE, secret=None, query={'key': ''})
    runs = wait_finished(wid, base + 2)[base:]
    nodes = [ended(r)[0] for r in runs]
    check('LEAD_WEBHOOK_SECRET left empty', 'fails closed: no secret and an empty ?key= are both refused with 401, nothing called',
          first[0] == 401 and second[0] == 401 and nodes == ['Stop: Bad Secret'] * 2
          and not [e for e in since(mark) if e['service'] != 'slack-errors'], f'{first[0]}, {second[0]}, ended at {nodes}')


def run_totals():
    """Every request the workflow made in the whole test, checked against what the real APIs accept."""
    bad = [e for e in LOG if e['service'] == 'monday' and e.get('invalid')]
    count = len([e for e in LOG if e['service'] == 'monday'])
    check('Every monday request', 'authenticated, JSON, the right API-Version, and valid against the monday GraphQL schema', count and not bad,
          f'{count} requests checked' + (f", first problem: {bad[0]['invalid']}" if bad else ''))
    texts = [e for e in LOG if e['service'] == 'twilio']
    odd = [e for e in texts if e['status'] != 201 and e['code'] != 21610]   # 21610 only where the test opted a number out
    check('Every Twilio request', 'accepted by the mock Twilio API, apart from the numbers the test opted out', texts and not odd,
          f'{len(texts)} requests checked' + (f", first problem: {odd[0]['code']}" if odd else ''))
    calls = [e for e in LOG if e['service'] == 'claude']
    odd = [e for e in calls if e['status'] not in (200, 529)]   # 529 only where the test made Claude overloaded
    check('Every Claude request', 'accepted by the mock Messages API (the real request shape was also checked with count_tokens)', calls and not odd,
          f'{len(calls)} requests checked')


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    started = time.time()
    server = ThreadingHTTPServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        err_env = {'SLACK_WEBHOOK_URL': MOCK + '/slack/errors'}
        error_id = create(TEMP_NAME, test_copy('unused', err_env, source=ERROR_WORKFLOW))
        wid_a = create(TEMP_NAME, test_copy(PATH_A, BASE_ENV, error_id))
        time.sleep(2)
        run_a(wid_a)
        run_a2(wid_a)

        # Quiet hours that end about three minutes from now, in a fixed UTC-5 zone. The escalation run must wait past 65 seconds,
        # so n8n parks it in the database and resumes it later (the same path a 5 minute timer takes in production).
        now_local = datetime.now(timezone.utc) - timedelta(hours=5)
        end_local = now_local.replace(second=0, microsecond=0) + timedelta(minutes=3)
        quiet = f"{(now_local - timedelta(hours=2)):%H:%M}-{end_local:%H:%M}"
        env_b = dict(BASE_ENV, QUIET_HOURS=quiet, TIMEZONE='Etc/GMT+5', ESCALATION_MINUTES='0.1', AGENTS_JSON=agents('Leo Park'))
        wid_b = create(TEMP_NAME, test_copy(PATH_B, env_b, error_id))
        time.sleep(2)
        run_b(wid_b, (end_local + timedelta(hours=5)).replace(tzinfo=timezone.utc))

        env_c = dict(BASE_ENV, AGENTS_JSON=agents('Maya Chen', 'Leo Park'), MONDAY_BOARD_ID=BOARD_C, MONDAY_GROUP_ID='',
                     MONDAY_COLUMNS=json.dumps({'status': 'status', 'agent': 'text_agent'}), DEFAULT_COUNTRY='GB',
                     TWILIO_FROM_NUMBER='', TWILIO_MESSAGING_SERVICE_SID=MESSAGING_SERVICE)
        wid_c = create(TEMP_NAME, test_copy(PATH_C, env_c, error_id))
        time.sleep(2)
        run_c(wid_c)

        # A typo in AGENTS_JSON (here: not JSON at all)
        wid_d = create(TEMP_NAME, test_copy(PATH_D, dict(BASE_ENV, AGENTS_JSON='[{name: Maya Chen}'), error_id))
        time.sleep(2)
        run_d(wid_d)

        wid_e = create(TEMP_NAME, test_copy(PATH_E, dict(BASE_ENV, LEAD_WEBHOOK_SECRET=''), error_id))
        time.sleep(2)
        run_e(wid_e)
        run_totals()
    finally:
        for wid in reversed(CREATED):
            try:
                api('POST', f'/api/v1/workflows/{wid}/deactivate')
            except Exception:
                pass
            try:
                api('DELETE', f'/api/v1/workflows/{wid}')
            except Exception as e:
                print(f'could not delete temporary workflow {wid}: {e}')
        server.shutdown()

    passed = sum(ok for _, _, ok, _ in RESULTS)
    last = None
    for scenario, label, ok, detail in RESULTS:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(RESULTS)} checks passed in {(time.time() - started) / 60:.1f} minutes')
    sys.exit(0 if passed == len(RESULTS) and RESULTS else 1)


if __name__ == '__main__':
    main()
