from __future__ import unicode_literals, print_function
import sys, csv, ast, os, logging, uuid, datetime, time, psycopg2, sqlparse, sqlite3, re, hoffparser
import simplejson as json
from flask import Flask, request, Response, render_template
from threading import Lock, Thread
from multiprocessing import Queue
from collections import defaultdict
from collections import OrderedDict
from pgcli.pgexecute import PGExecute
from pgspecial import PGSpecial
from pgspecial.main import (PGSpecial, NO_QUERY, PAGER_OFF)
from hoffimporter import HoffImporter
from hoffeye import HoffEye
from pgcli.pgcompleter import PGCompleter
from pgcli.completion_refresher import CompletionRefresher
from prompt_toolkit.document import Document
from itsdangerous import Serializer
from os.path import expanduser

try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse
special = PGSpecial()

from psycopg2.extensions import (TRANSACTION_STATUS_IDLE,
                                TRANSACTION_STATUS_ACTIVE,
                                TRANSACTION_STATUS_INTRANS,
                                TRANSACTION_STATUS_INERROR,
                                TRANSACTION_STATUS_UNKNOWN,
                                STATUS_READY,
                                STATUS_IN_TRANSACTION,
                                STATUS_PREPARED)

if sys.version_info[0] < 3: # Python 3 uses UTF-8 by default
    reload(sys)
    sys.setdefaultencoding('utf8')

home_dir = os.path.expanduser('~/.pghoffserver')
completers = defaultdict(list)  # Dict mapping urls to pgcompleter objects
completer_lock = Lock()
completerSettings = defaultdict(list)
executors = defaultdict(list)  # Dict mapping buffer ids to pgexecutor objects
hoffeyes = defaultdict(list)

executor_lock = defaultdict(lambda: Lock())
bufferConnections = defaultdict(str) #Dict mapping bufferids to connectionstrings
queryResults = defaultdict(list)
dbSyncQueue = Queue()
type_dict = defaultdict(dict)
config = {}
serverList = {}
uuids_pending_execution = []
result_cache = defaultdict(list)
executor_queues = defaultdict(lambda: Queue())
db_name = 'hoff.db'

logger = logging.getLogger('mylogger')

def my_handler(type, value, tb):
    logger.exception("Uncaught exception: {0}".format(str(value)))

sys.excepthook = my_handler

def main():
    global serverList
    global config
    global apikey
    global completerSettings

    #make sure ~/.config/pgcli directory exists
    pgcli_dir = expanduser("~/.config/pgcli")
    if not os.path.exists(pgcli_dir):
        os.makedirs(pgcli_dir)

    # Stop psycopg2 from mangling intervals
    psycopg2.extensions.register_type(psycopg2.extensions.new_type(
        (1186,), str("intrvl"), lambda val, cur: val))

    if not os.path.exists(home_dir):
        os.makedirs(home_dir)
    try:
        with open(home_dir + '/.key', mode='r+') as api_key_file:
            apikey = api_key_file.readLine()
            if not apikey:
                apikey = to_str(uuid.uuid1())
                api_key_file.write(apikey)
    except Exception:
        try:
            with open(home_dir + '/.key', mode='w') as api_key_file:
                apikey = to_str(uuid.uuid1())
                api_key_file.write(apikey)
        except Exception as e:
            print ('Error generating API-key ' + to_str(e))
            sys.exit(0)
    try:
        with open(home_dir + '/config.json') as json_data_file:
            config = json.load(json_data_file, object_pairs_hook=OrderedDict)
            #Todo: load PGCLI using site-dirs from config file.
            serverList = config['connections']
            completerSettings = dict([ (a, c.get('completer_settings')) for (a, c) in config['connections'].items()])
    except Exception as e:
        config = dict()
        serverList = dict()
    init_db()
    #start cleanup worker
    t = Thread(target=cleanup_worker,
                   name='cleanup_worker')
    t.setDaemon(True)
    t.start()

def init_db():
    conn = sqlite3.connect(home_dir + '/' + db_name)
    cur = conn.cursor()
    cur.execute("pragma user_version")
    row = cur.fetchone()
    dbversion = (row[0])

    sql = """CREATE TABLE IF NOT EXISTS QueryData(
      alias text, batchid text, queryid text, dynamic_table_name text, columns text, rows text,
      query text, notices text, statusmessage text,
      runtime_seconds int, error text,
      datestamp timestamp
    )"""
    conn.execute(sql)
    sql = """CREATE TABLE IF NOT EXISTS CellOperations(
      name text, colloperationcell text, celloperationtype, query text
    )"""
    conn.execute(sql)
    sql = """CREATE TABLE IF NOT EXISTS CellOperationChecks(
      celloperationcell text, checkname text, validationcell text,
      comparisoncell text, comparisonvalue text, operator text
    )"""
    conn.execute(sql)

    if dbversion == 0:
        sql = "ALTER TABLE QueryData ADD column messages text"
        conn.execute(sql)
        sql = "pragma user_version = 1"
        conn.execute(sql)

    conn.close()
    t = Thread(target=db_worker,
                   name='db_worker')
    t.setDaemon(True)
    t.start()

def db_worker():
    conn = sqlite3.connect(home_dir + '/' + db_name)
    while True:
        dbitem = dbSyncQueue.get(block=True)
        if dbitem['operation'] == 'write':
            r = dbitem['data']
            conn.cursor().execute("INSERT INTO QueryData VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (r['alias'], r['batchid'], r['queryid'], None, json.dumps(r['columns']), json.dumps(r['rows'], default=str),
                r['query'], json.dumps(r['notices']),
                r['statusmessage'], r['runtime_seconds'], r['error'], r['timestamp'], json.dumps(r.get('messages'), default=str)))
            conn.commit()
        if dbitem['operation'] == 'delete_query':
            conn.cursor().execute("DELETE QueryData WHERE alias = :a AND datestamp < date('now','-:d days')",
                                 ({'a':dbitem['alias'], 'd':dbitem['days']}));
            conn.commit()
        if dbitem['operation'] == 'delete_querydata':
            conn.cursor().execute("UPDATE QueryData SET rows = NULL WHERE alias = :a AND datestamp < date('now','-:d days')",
                                 ({'a':dbitem['alias'], 'd':dbitem['days']}));
            conn.commit()

def cleanup_worker():
    while True:
        time.sleep(10)
        toremove = []
        for (uuid, cache) in [(uuid, cache) for (uuid, cache) in result_cache.items() if time.time() - cache['time'] >= 10]:
            if cache['save_to_disk']:
                dbSyncQueue.put({'operation':'write', 'data':cache['result']}, block=False)
            del result_cache[uuid]
        for alias in serverList:
            days = serverList[alias].get('data_retention', 7)
            if days:
                dbSyncQueue.put({'operation':'delete_querydata', 'alias': alias, 'days': days}, block=False)
            days = serverList[alias].get('query_retention')
            if days:
                dbSyncQueue.put({'operation':'delete_query', 'alias': alias, 'days': days}, block=False)

def to_str(string):
    if sys.version_info < (3,0):
         return unicode(string)
    return str(string)

def generateTable (tbl, borderHorizontal = '-', borderVertical = '|', borderCross = '+'):
    cols = [list(x) for x in zip(*tbl)]
    lengths = [max(map(len, map(str, col))) for col in cols]
    f = borderVertical + borderVertical.join(' {:>%d} ' % l for l in lengths) + borderVertical
    s = borderCross + borderCross.join(borderHorizontal * (l+2) for l in lengths) + borderCross
    ret = s
    for row in tbl:
        ret += '\n' + (f.format(*row))
        ret += '\n' + s
    return ret

def read_snippets_config():
    try:
        with open(home_dir + '/snippets.json') as json_data_file:
            snippets = json.load(json_data_file, object_pairs_hook=OrderedDict)
            if snippets == None:
                snippets = {}

            return snippets
    except Exception as e:
        return {}

def write_snippets_config(c):
    with open(home_dir + '/snippets.json', mode='w') as configfile:
        json.dump(c, configfile)

def write_config():
    with open(home_dir + '/config.json', mode='w') as configfile:
        json.dump(config, configfile)

def new_server(alias, url, requiresauthkey):
    serverList[alias] = {'url':url, 'requiresauthkey':requiresauthkey}
    config['connections'] = serverList
    write_config()

def remove_server(alias):
    if config['connections'].get(alias):
        del config['connections'][alias]
    if serverList.get(alias):
        del serverList[alias]
    write_config()

def start_hoffeye(alias, query, authkey=None):
    print('starting hoffeye')
    id = None
    if hoffeyes[alias] and query:
        id = hoffeyes[alias].add_watch(query)
    else:
        server = serverList.get(alias, None)
        dsn = server.get('dsn')
        executor = new_executor(server['url'], dsn, authkey)
        hoffeyes[alias] = HoffEye(executor)
        if query:
            id = hoffeyes[alias].add_watch(query)
        hoffeyes[alias].start()
    return id

def stop_hoffeye(alias):
    if hoffeyes[alias]:
        hoffeyes[alias].stop()

def clear_hoffeye(alias):
    if hoffeyes[alias]:
        hoffeyes[alias].clear()

def remove_hoffeye(alias, id):
    if hoffeyes[alias]:
        hoffeyes[alias].remove_watch(id)

def connect_server(alias, authkey=None):
    completer_settings = {
        'generate_aliases' : True,
        'casing_file' : os.path.expanduser('~/.config/pgcli/casing'),
        'generate_casing_file' : True,
        'single_connection': True,
        "call_arg_style": "{arg_name} := ${{{arg_num}:{arg_default}}}",
        "call_arg_display_style": "{arg_name}:={arg_default}",
        "call_arg_oneliner_max": 2,
        "signature_arg_style": "{arg_name} {arg_type}"
    }
    completer_settings.update(completerSettings.get(alias, {}))
    completerSettings[alias] = completer_settings
    server = serverList.get(alias, None)
    if not server:
        return {'alias': alias, 'success':False, 'errormessage':'Unknown alias.'}
    if executors[alias]:
        return {'alias': alias, 'success':False, 'errormessage':'Already connected to server.'}
    refresher = CompletionRefresher()
    history = [x['query'] for x in search_query_history('', False, 'query', 300, 'DESC')[:-1:299]]
    try:
        with executor_lock[alias]:
            dsn = server.get('dsn')
            executor = new_executor(server['url'], dsn, authkey)
            with executor.conn.cursor() as cur:
                cur.execute('SELECT oid, oid::regtype::text FROM pg_type')
                type_dict[alias] = dict(row for row in cur.fetchall())
            executors[alias] = executor
            refresher.refresh(
                executor, special=special, history=history, callbacks=(
                    lambda c: swap_completer(c, alias)
                ),
                settings=completerSettings[alias]
            )
            serverList[alias]['connected'] = True
    except psycopg2.Error as e:
        return {'success':False, 'errormessage':to_str(e)}

    #wait for connection to be established
    sleep = 0
    while True:
        time.sleep(0.01)
        sleep += 0.01
        if sleep >= 5:
            return {'alias': alias, 'success':False, 'errormessage':'Connection timed out.'}
        elif executors[alias].conn.get_transaction_status() == TRANSACTION_STATUS_IDLE and executors[alias].conn.status == STATUS_READY:
            time.sleep(0.5)
            break

    #create a queue for this alias and start a worker thread
    executor_queues[alias] = Queue()
    t = Thread(target=executor_queue_worker,
                   args=(alias,),
                   name='executor_queue_worker')
    t.setDaemon(True)
    t.start()

    return {'alias': alias, 'success':True, 'color': config['connections'][alias].get('color'), 'errormessage':None}

def update_completer_settings(alias, new_settings):
    if new_settings != completerSettings[alias]:
        completerSettings[alias].update(new_settings)
        refresher = CompletionRefresher()
        refresher.refresh(executors[alias], special=special, callbacks=(
                                        lambda c: swap_completer(c, alias, True)), settings=completerSettings[alias])

def refresh_servers():
    for alias, server in serverList.items():
        if alias in executors:
            with executor_lock[alias]:
                try:
                    if executors.get(alias).conn.closed == 0:
                        server['connected'] = True
                    else:
                        server['connected'] = False
                        del executors[alias]
                except Exception:
                    server['connected'] = False
                    del executors[alias]
        else:
            server['connected'] = False
        if alias in hoffeyes:
            try:
                if hoffeyes.get(alias).executor.conn.closed == 0:
                    server['hoffeye_connected'] = True
                    print('connected!')
                else:
                    print('disconnected!')
                    server['hoffeye_connected'] = False
                    del hoffeyes[alias]
            except Exception:
                print('EXCEPTION!', Exception)
                server['hoffeye_connected'] = False
                del hoffeyes[alias]
        else:
            server['hoffeye_connected'] = False

def server_status(alias):
    with executor_lock[alias]:
        server = next((s for (a, s) in serverList.items() if a == alias), None)
        if not server:
            return {'alias':alias, 'guid':None, 'success':False, 'errormessage':'Unknown alias.'}
        if executors[alias]:
            if executors[alias].conn.closed == 1:
                server['connected'] = False
                del executors[alias]
        if not executors[alias]:
            return {'alias':alias, 'guid':None, 'success':False, 'Url':None, 'errormessage':'Not connected.'}
        return {'success':True}

def disconnect_server(alias):
    if alias not in executors:
        return {'success':False, 'errormessage':'Not connected.'}
    server = serverList.get(alias, None)
    if not server:
        return {'success':False, 'errormessage':'Unknown alias.'}
    else:
        server['connected'] = False
        executors[alias].conn.cancel()
        executors[alias].conn.close()
        del executors[alias]
    return {'success':True, 'errormessage':None}

def cancel_execution(alias):
    if alias not in executors:
        return {'success':False, 'errormessage':'Not connected.'}
    server = serverList.get(alias, None)
    if not server:
        return {'success':False, 'errormessage':'Unknown alias.'}
    else:
        with executor_lock[alias]:
            for uuid in queryResults:
                r = queryResults[uuid]
                if not r:
                    return {'success':False, 'errormessage':None}
                if r['alias'] == alias and r['complete'] == False:
                    r['error'] = 'canceling statement due to user request'
                    r['executing'] = False
                    r['complete'] = True
            while not executor_queues[alias].empty():
                executor_queues[alias].get(block=True)
            executors[alias].conn.cancel()
            executors[alias].conn.rollback()

    return {'success':True, 'errormessage':None}

def new_executor(url, dsn=None, pwd=None, settings=None):
    uri = urlparse(url)
    database = uri.path[1:]  # ignore the leading fwd slash
    return PGExecute(database, uri.username, pwd or uri.password, uri.hostname, uri.port, dsn)

def swap_completer(comp ,alias, save_settings = False):
    completers[alias] = comp
    if save_settings:
        for k, v in serverList.items():
            #v.setdefault('completer_settings', completerSettings[k])
            v['completer_settings'] = completerSettings[k]
        write_config()

def get_transaction_status_text(status):
    return {
        TRANSACTION_STATUS_IDLE: 'idle',
        TRANSACTION_STATUS_ACTIVE: 'active',
        TRANSACTION_STATUS_INTRANS: 'intrans',
        TRANSACTION_STATUS_INERROR: 'inerror',
        TRANSACTION_STATUS_UNKNOWN: 'unknown'
    }[status]

def refresh_metadata(alias, cur):
    cur.execute('SELECT oid, oid::regtype::text FROM pg_type')
    type_dict[alias] = dict(row for row in cur.fetchall())

def refresh_completer(alias):
    try:
        refresher = CompletionRefresher()
        refresher.refresh(executors[alias], special=special, callbacks=(
                                lambda c: swap_completer(c, alias)), settings=completerSettings[alias])
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')
    except:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Could not refresh metadata.'})), mimetype='text/json')

def queue_query(alias, sql, cursor_pos):
    queryids = []
    batchid = to_str(uuid.uuid1())
    statementlength = 0
    query_found = False
    for sql in sqlparse.split(sql if sql else ''):
        statementlength += len(sql)
        if cursor_pos:
            if cursor_pos >= statementlength:
                continue
            elif cursor_pos < statementlength and query_found:
                continue
            query_found = True
        queryid = to_str(uuid.uuid1())
        queryResults[queryid] = {
            'alias': alias,
            'batchid': batchid,
            'queryid': queryid,
            'columns': None,
            'rows': None,
            'query': sql,
            'notices': None,
            'statusmessage': None,
            'complete': False,
            'executing': False,
            'timestamp': None,
            'runtime_seconds': None,
            'error':None,
            'transaction_status':None,
            'dynamic_alias': None
        }
        executor_queues[alias].put({'uuid': queryid})
        queryids.append(queryid)
    return {'queryids': queryids, 'batchid': batchid}

def create_import(hoffimportfile):
    import_queue = []
    queryids = []
    importer = HoffImporter(hoffimportfile)
    from_queries = importer.get_from_queries()
    to_queries = importer.get_to_queries()
    sequential_queries = []
    sequential_queries.append(from_queries)
    sequential_queries.append(to_queries)

    batchid = to_str(uuid.uuid1())
    for sq in sequential_queries:
        for query in sq['queries']:
            queryid = to_str(uuid.uuid1())
            queryResults[queryid] = {
                'alias': sq['alias'],
                'batchid': batchid,
                'queryid': queryid,
                'columns': None,
                'rows': None,
                'query': query,
                'notices': None,
                'statusmessage': None,
                'complete': False,
                'executing': False,
                'timestamp': None,
                'runtime_seconds': None,
                'error':None,
                'transaction_status':None,
                'dynamic_alias': None
            }
            import_queue.append({'alias': sq['alias'], 'uuid': queryid})
            queryids.append(queryid)

    t = Thread(target=import_worker,
                   args=(import_queue,),
                   name='import_worker')
    t.setDaemon(True)
    t.start()

    return batchid, queryids

def import_worker(import_queue):
    for queue in import_queue:
        executor_queues[queue['alias']].put({'uuid': queue['uuid']})
        while(True):
            if not queryResults[queue['uuid']] or queryResults[queue['uuid']]['complete']:
                break
            time.sleep(0.1)

def executor_queue_worker(alias):
    executor = executors[alias]
    if not executor:
        return
    while executors[alias].conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
        time.sleep(2)
    if executor.conn.closed != 2:
        time.sleep(2)
    #pick up work from queue
    while alias in serverList and serverList[alias].get('connected'):
        queue = executor_queues[alias].get(block=True)
        uid = queue['uuid']
        try:
            with executor.conn.cursor() as cur:
                completer = completers[alias]
                if executors[alias].conn.get_transaction_status() != TRANSACTION_STATUS_INERROR:
                    refresh_metadata(alias, cur)
                timestamp_ts = time.mktime(datetime.datetime.now().timetuple())
                currentQuery = queryResults[uid]
                currentQuery['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
                currentQuery['executing'] = True
                currentQuery['messages'] = []
                queryResults[uid] = currentQuery
                #Check if there are any dynamic tables in the query
                query = update_query_with_dynamic_tables(queryResults[uid]['query'])
                if len(query) == 0 and len(queryResults[uid]['query']) > 0:
                    currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                    currentQuery['complete'] = True
                    currentQuery['executing'] = False
                    currentQuery['statusmessage'] = 'DROP DYNAMIC'
                    queryResults[uid] = currentQuery
                    continue
                #run query
                try:
                    # First try to run each query as special
                    if special:
                        messages = []
                        gotmessage = False
                        try:
                            for result in special.execute(cur, query):
                                if result[1] and result[2]:
                                    x = [result[2]] + [x for x in result[1]]
                                    messages.append(generateTable(x) + '\n' + result[3])
                            currentQuery['messages'] = messages
                            currentQuery['statusmessage'] = query
                            gotmessage = True
                        except Exception as e:
                            if len(e.args) > 0:
                                gotmessage = True
                                currentQuery['error'] = to_str(e)
                        if gotmessage:
                            currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                            currentQuery['complete'] = True
                            currentQuery['executing'] = False
                            queryResults[uid] = currentQuery
                            continue
                    cur.execute(query)
                except psycopg2.Error as e:
                    currentQuery['error'] = to_str(e)

                if cur.description and not currentQuery['error']:
                    case = completer.case if completer else lambda x: x
                    columns = [
                        {
                            'name': case(d.name),
                            'type_code': d.type_code,
                            'type': type_dict[alias][d.type_code],
                            'field':case(d.name) + str(i),
                            'data_length': 0
                        } for i, d in enumerate(cur.description, 1)
                    ]
                    currentQuery['columns'] = columns
                    currentQuery['rows'] = []
                    for row in cur.fetchall():
                        rowdict = {}
                        currentQuery['rows'].append(rowdict)
                        for col, data in zip(columns, row):
                            rowdict[case(col["field"])] = data
                            col['data_length'] = max(len(to_str(data)), col['data_length'])
                #update query result
                currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                currentQuery['complete'] = True
                currentQuery['executing'] = False
                currentQuery['statusmessage'] = cur.statusmessage
                notices = []
                while executor.conn.notices:
                    notices.append(executor.conn.notices.pop(0))
                currentQuery['notices'] = notices
                queryResults[uid] = currentQuery
        except Exception as e:
            currentQuery = queryResults[uid]
            currentQuery['error'] = to_str(e)
            currentQuery['complete'] = True

def update_query_with_dynamic_tables(query):
    dynamic_tables = list_dynamic_tables()
    if not dynamic_tables:
        return query
    for x in dynamic_tables:
        if ('drop dynamic ##' + x['dynamic_table_name']) in re.sub(r"(\s+)", " ", query, 0).lower():
            delete_dynamic_table(x['dynamic_table_name'])
            query = ''
        elif '##' + x['dynamic_table_name'] in query:
            query = query.replace('##' + x['dynamic_table_name'], construct_dynamic_table(x['dynamic_table_name']))
    return query

def get_word(text, position):
    index = text.rfind("##", 0, int(position))
    if index > -1:
        return text[index + 1:int(position)]

def find_dynamic_table(query, pos):
    searchstring = get_word(query, pos)
    if not searchstring:
        return None
    searchstring = re.sub(r'\W+', '', searchstring)

    dynamic_tables = list_dynamic_tables()
    if not dynamic_tables:
        return None
    result = []
    for x in dynamic_tables:
        if x['dynamic_table_name'].find(searchstring) > -1:
            result.append(x['dynamic_table_name'])
    return result

def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]
    return d

def fetch_result(uuid, rows_from=None, rows_to=None):
    result = queryResults[uuid] or result_cache and result_cache[uuid]['result']
    if not result: #look for result in db
        conn = sqlite3.connect(home_dir + '/' + db_name)
        conn.row_factory = dict_factory
        cur = conn.cursor()
        cur.execute("SELECT * FROM QueryData WHERE queryid = ?", (to_str(uuid),))
        row = cur.fetchone()
        if row:
            try:
                transactiontext = get_transaction_status_text(executors[row['alias']].conn.get_transaction_status())
            except:
                transactiontext = None
            rowdata = json.loads(row["rows"])
            result = {
                'alias': row["alias"],
                'batchid': row['batchid'],
                'queryid': row['queryid'],
                'columns': json.loads(row["columns"]),
                'rows': rowdata or [],
                'query': row["query"],
                'notices': json.loads(row["notices"]),
                'statusmessage': row["statusmessage"],
                'messages': row['messages'],
                'complete': True,
                'executing': False,
                'timestamp': row["datestamp"],
                'runtime_seconds': row["runtime_seconds"],
                'transaction_status': transactiontext,
                'rowcount': len(rowdata) if rowdata else None,
                'error': row["error"]
            }
            if rows_from and rows_to:
                result_cache[uuid] = {'time': time.time(), 'save_to_disk': False, 'result': result}
                result['rowcount'] = len(rowdata) if rowdata else 0
                rowdata = rowdata[min([int(rows_from), len(rowdata)]):min([int(rows_to), len(rowdata)])] if rowdata else None
                result["rows"] = rowdata
            return Response(to_str(json.dumps(result)), mimetype='text/json')
        else:
            return Response(to_str(json.dumps({'success':False, 'errormessage':'Unknown queryid.'})), mimetype='text/json')
    try:
        if result['executing'] == True:
            timestamp_ts = time.mktime(datetime.datetime.strptime(result["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
            result["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
        result['transaction_status'] = get_transaction_status_text(executors[result['alias']].conn.get_transaction_status())
        if result['complete'] == True: #put result in queue for db-storage
            if rows_from and rows_to:
                if not result_cache[uuid]:
                    result_cache[uuid] = {'time': time.time(), 'save_to_disk':True, 'result': result}
                    del queryResults[uuid]
            else:
                dbSyncQueue.put({'operation':'write', 'data':result})
                del queryResults[uuid]
        if rows_from and rows_to:
            partialrows = result["rows"][min([int(rows_from), len(result["rows"])]) : min([int(rows_to), len(result["rows"])])] if result["rows"] else None
            partialresult = {
                'alias': result["alias"],
                'batchid': result['batchid'],
                'queryid': result['queryid'],
                'columns': result["columns"],
                'rows': partialrows or [],
                'query': result["query"],
                'notices': result["notices"],
                'statusmessage': result["statusmessage"],
                'messages': result['messages'],
                'complete': result['complete'],
                'executing': result['executing'],
                'timestamp': result["timestamp"],
                'runtime_seconds': result["runtime_seconds"],
                'transaction_status': result['transaction_status'],
                'rowcount': len(result["rows"]) if result["rows"] else 0,
                'error': result["error"]
            }
            return Response(to_str(json.dumps(partialresult, default=str)), mimetype='text/json')
        return Response(to_str(json.dumps(result, default=str)), mimetype='text/json')
    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected.', 'actual_error' : str(e)})), mimetype='text/json')

def create_dynamic_table(queryid, name):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.cursor().execute('UPDATE QueryData SET dynamic_table_name = ? WHERE queryid = ?;', (name, to_str(queryid)))
    conn.commit()
    conn.close()
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

def delete_dynamic_table(table_name):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    where_sql = ' WHERE dynamic_table_name = ?;'
    param = table_name
    conn.cursor().execute('UPDATE QueryData SET dynamic_table_name = NULL' + where_sql, (param,))
    conn.commit()
    conn.close()
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

def list_dynamic_tables(alias = None):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.row_factory = dict_factory
    cur = conn.cursor()
    if alias:
        cur.execute('SELECT * FROM QueryData WHERE dynamic_table_name IS NOT NULL AND alias = ?;', (alias,))
    else:
        cur.execute('SELECT alias, batchid, queryid, dynamic_table_name FROM QueryData WHERE dynamic_table_name IS NOT NULL;')
    results = cur.fetchall()
    conn.close()
    return results

def construct_dynamic_table(dynamic_table_name):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.row_factory = dict_factory
    cur = conn.cursor()
    cur.execute('SELECT * FROM QueryData WHERE dynamic_table_name = ?;', (dynamic_table_name,))
    result = cur.fetchone()
    if not result:
        return None
    rows = json.loads(result["rows"])
    columnheaders = json.loads(result["columns"])
    output = []
    sql = ''
    for row in rows:
        values = []
        for header in columnheaders:
            if row[header['field']]:
                if header['type'] in ('integer', 'bigint', 'numeric', 'smallint'):
                    values.append(to_str(row[header['field']]))
                else:
                    values.append("'" + to_str(row[header['field']]).replace("'", "''") + "'")
            else:
                values.append('NULL')
        output.append(','.join(values))
    sql += "),(".join(str(column) for column in output)
    sql = '(SELECT * FROM (VALUES(' + sql + ')) DT (' + ",".join(str(column['name']) for column in columnheaders) + '))'
    return sql

def search_query_history(q, search_data=False, columns=None, limit=None, reverse_order=True):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.row_factory = dict_factory
    cur = conn.cursor()
    cur.execute("SELECT alias, query, runtime_seconds, datestamp as timestamp, batchid, queryid"
        + " FROM QueryData WHERE query LIKE :q "
        + (" OR rows LIKE :q" if search_data else "")
        + " ORDER BY datestamp " + ('DESC' if reverse_order else "ASC") + (" LIMIT :l" if limit else ";"),
        ({"q":'%' + q + '%', "l":limit}))
    result = cur.fetchall()
    return result

def get_meta_data(alias, name):
    comps = completers[alias].get_completions(
                Document(text='select * from bank', cursor_position=18), None)
    print(comps, file=sys.stderr)

def write_csv_file(queryid, options, path):
    if(not os.path.isdir(os.path.dirname(os.path.abspath(path)))):
        return False

    result = queryResults[queryid] or result_cache and result_cache[queryid]['result']
    rows = None
    columns = None
    if result:
        rows = result["rows"]
        columns = result["columns"]
    if not result:
        conn = sqlite3.connect(home_dir + '/' + db_name)
        conn.row_factory = dict_factory
        cur = conn.cursor()
        cur.execute('SELECT * FROM QueryData WHERE queryid = ?;', (queryid,))
        result = cur.fetchone()
        rows = json.loads(result["rows"])
        columns = json.loads(result["columns"])

    if not result:
        return False

    fieldnames = [x['name'] for x in columns]
    with open(path, 'w') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, delimiter=(str(options.get('delimiter') or ';') if options else str(';')), quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            out = '{'
            for column in columns:
                if row[column['field']]:
                    out += "'''" + to_str(column['name']) + "''':'''" + to_str(row[column['field']]) + "''',"

            out = out[:-1] + '}'
            writer.writerow(ast.literal_eval(out))

    return True

app = Flask(__name__)
@app.route("/query", methods=['POST'])
def app_query():
    alias = request.form.get('alias', 'Vagrant')
    sql = request.form['query']
    cursor_pos = request.form.get('cursor_pos', None)
    if cursor_pos:
        try:
            cursor_pos = int(cursor_pos)
        except:
            return Response(to_str(json.dumps({'success':False, 'errormessage':'Invalid cursor position'})), mimetype='text/json')

    sstatus = server_status(alias)
    if not sstatus['success']:
        return Response(to_str(json.dumps(sstatus)), mimetype='text/json')
    if not executors[alias].conn.status == STATUS_READY:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Already executing query'})), mimetype='text/json')
    queryqueue = queue_query(alias, sql, cursor_pos)
    queryids = queryqueue['queryids']
    batchid = queryqueue['batchid']
    urls = []
    for qid in queryids:
        urls.append('localhost:5000/result/' + qid)
    return Response(to_str(json.dumps({'success':True, 'batchid':batchid, 'queryids':queryids, 'Urls':urls, 'errormessage':None})), mimetype='text/json')

@app.route("/result/<uuid>")
def app_result(uuid):
    return fetch_result(uuid)

@app.route("/result/<uuid>/<rows_from>/<rows_to>")
def app_partial_result(uuid, rows_from, rows_to):
    return fetch_result(uuid, rows_from, rows_to)

@app.route("/executing")
def app_executing():
    output = []
    uuid_delete = []
    for uuid in queryResults:
        r = queryResults[uuid]
        if r['executing']:
            timestamp_ts = time.mktime(datetime.datetime.strptime(r["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
            r["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
        r['transaction_status'] = get_transaction_status_text(executors[r['alias']].conn.get_transaction_status())
        output.append(r)
        if r['complete']:
            dbSyncQueue.put({'operation':'write', 'data':queryResults[uuid]})
            uuid_delete.append(uuid)
    for uuid in uuid_delete:
        del queryResults[uuid]
    return Response(to_str(json.dumps(output)), mimetype='text/json')

@app.route("/completions", methods=['POST'])
def app_completions():
    pos = request.form['pos']
    query = request.form['query']
    alias = request.form.get('alias', 'Vagrant')
    dynamic_tables_match = find_dynamic_table(query, pos)
    dt_out = []
    if alias in completers:
        if dynamic_tables_match:
            dt_out = [{'text': c, 'type': 'Dynamic table', 'displayText': c} for c in dynamic_tables_match]
        comps = completers[alias].get_completions(
                    Document(text=query, cursor_position=int(pos)), None)
        comps_out = [{'text': c.text, 'type': c._display_meta, 'displayText': c.display} for c in comps]
        out = dt_out + comps_out
        return Response(to_str(json.dumps(out)), mimetype='text/json')
    return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected to server.'})), mimetype='text/json')

@app.route("/listservers")
def app_list_servers():
    refresh_servers()
    return Response(to_str(json.dumps(serverList)), mimetype='text/json')

@app.route("/listconnections")
def list_connections():
    return Response(to_str(json.dumps(get_connections(), indent=4)), mimetype='text/json')

@app.route("/connect", methods=['POST'])
def app_connect():
    alias = request.form['alias']
    authkey = request.form.get('authkey')
    return Response(to_str(json.dumps(connect_server(alias, authkey))), mimetype='text/json')

@app.route("/addserver", methods=['POST'])
def app_addserver():
    alias = request.form['alias']
    if next((s for (a, s) in serverList.items() if a == alias), None):
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Server alias already exists.'})), mimetype='text/json')
    else:
        url = request.form['url']
        requiresauthkey = request.form['requiresauthkey']
        new_server(alias, url, requiresauthkey)
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/delserver", methods=['POST'])
def app_delserver():
    try:
        alias = request.form['alias']
        remove_server(alias)
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')
    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage':to_str(e)})), mimetype='text/json')

@app.route("/disconnect", methods=['POST'])
def app_disconnect():
    try:
        alias = request.form['alias']
        disconnect_server(alias)
        return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')
    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage':to_str(e)})), mimetype='text/json')

@app.route("/cancel", methods=['POST'])
def app_cancel():
    try:
        alias = request.form['alias']
        if alias in executors and executors[alias].conn.get_transaction_status() == TRANSACTION_STATUS_IDLE:
            return Response(to_str(json.dumps({'success':False, 'errormessage':'Not executing'})), mimetype='text/json')
        return Response(to_str(json.dumps(cancel_execution(alias))), mimetype='text/json')

    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage':to_str(e)})), mimetype='text/json')

@app.route("/create_dynamic_table", methods=['POST'])
def app_create_dynamic_table():
    queryid = request.form['queryid']
    name = request.form['name']
    return create_dynamic_table(queryid, name)

@app.route("/delete_dynamic_table", methods=['POST'])
def app_delete_dynamic_table():
    uuid = request.form['uuid']
    return delete_dynamic_table(uuid, None)

@app.route("/delete_dynamic_tables", methods=['POST'])
def app_delete_dynamic_tables():
    alias = request.form.get('alias')
    return delete_dynamic_table(None, alias)

@app.route("/list_dynamic_tables", methods=['POST'])
def app_list_dynamic_tables():
    alias = request.form.get('alias')
    dynamic_tables =  list_dynamic_tables(alias)
    if dynamic_tables:
        return Response(to_str(json.dumps(dynamic_tables)), mimetype='text/json')
    else:
        return Response(to_str(json.dumps(None)), mimetype='text/json')

@app.route("/export_dynamic_table", methods=['POST'])
def app_export_dynamic_table():
    name = request.form['name']
    return Response(to_str(construct_dynamic_table(name)), mimetype='text')

@app.route("/get_metadata", methods=['POST'])
def get_metadata():
    sql = request.form['sql']
    return Response(to_str(json.dumps(hoffparser.extract_tables(sql))), mimetype='text')

@app.route("/refresh_definitions", methods=['POST'])
def app_refresh_completer():
    alias = request.form.get('alias')
    sstatus = server_status(alias)
    if not sstatus['success']:
        return Response(to_str(json.dumps(sstatus)), mimetype='text/json')
    return refresh_completer(alias)

@app.route("/query_status/<uuid>")
def query_status(uuid):
    querystatus = {
        'complete': queryResults[uuid]['complete'],
        'query': queryResults[uuid]['query'],
        'batchid': queryResults[uuid]['batchid'],
        'queryid': queryResults[uuid]['queryid'],
        'has_result': True if queryResults[uuid]['columns'] and len(queryResults[uuid]['columns']) > 0 else False,
        'has_notices': True if queryResults[uuid]['notices'] else False,
        'has_error': True if queryResults[uuid]['error'] else False,
        'has_queryplan': True if queryResults[uuid]['columns'] and
                                 len(queryResults[uuid]['columns']) == 1 and
                                 queryResults[uuid]['columns'][0]['name'] == 'QUERY PLAN' and
                                 queryResults[uuid]['statusmessage'] == 'EXPLAIN' else False
        }
    return Response(to_str(json.dumps(querystatus)), mimetype='text/json')

@app.route("/search", methods=['POST'])
def app_search():
    q = request.form['q']
    search_data = request.form.get('search_data') == 'True'
    result = search_query_history(q, search_data)
    if not result:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'No queries match the given search criteria.'})), mimetype='text/json')
    return Response(to_str((json.dumps(result))), mimetype='text/json')

@app.route("/write_csv_file", methods=['POST'])
def app_write_csv_file():
    queryid = request.form['queryid']
    options = request.form.get('options')
    path = request.form['path']
    result = write_csv_file(queryid, options, path)
    if not result:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Could not write csv file.'})), mimetype='text/json')
    return Response(to_str(json.dumps({'success':True})), mimetype='text/json')

@app.route("/get_meta_data", methods=['POST'])
def app_get_meta_data():
    alias = request.form.get('alias')
    name = request.form.get('name')
    if alias not in serverList:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Unknown alias.'})), mimetype='text/json')
    if alias not in executors:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Not connected.'})), mimetype='text/json')
    if not name:
        return Response(to_str(json.dumps({'success':False, 'errormessage':'No object specified.'})), mimetype='text/json')
    return Response(to_str(get_meta_data(alias, name)), mimetype='text/json')

@app.route("/get_settings", methods=['POST'])
def app_get_completer_settings():
    alias = request.form.get('alias')
    return Response(to_str(json.dumps(completerSettings[alias])), mimetype='text/json')

@app.route("/update_settings", methods=['POST'])
def app_update_completer_settings():
    alias = request.form.get('alias')
    settings = request.form.get('settings')
    try:
        settings = json.loads(settings)
    except:
        return Response(to_str(json.dumps({'success':False, 'errormessage': 'Unknown settings.'})), mimetype='text/json')
    try:
        update_completer_settings(alias, settings)
    except Exception as e:
        return Response(to_str(json.dumps({'success':False, 'errormessage': 'Not connected.'})), mimetype='text/json')
    return Response(to_str(json.dumps({'success':True, 'errormessage': None})), mimetype='text/json')

@app.route("/hoff_import", methods=['POST'])
def app_hoff_import():
    hoffimportfile = request.form.get('hoffimportfile')
    if not hoffimportfile:
        return Response(to_str(json.dumps({'success':False, 'errormessage': 'Unknown hoffimport file.'})), mimetype='text/json')

    batchid, queryids = create_import(hoffimportfile)
    urls = []
    for qid in queryids:
        urls.append('localhost:5000/result/' + qid)
    return Response(to_str(json.dumps({'success':True, 'batchid':batchid, 'queryids':queryids, 'Urls':urls, 'errormessage':None})), mimetype='text/json')

@app.route("/hoffeye_new", methods=['POST'])
def new_hoffeye():
    alias = request.form.get('alias')
    authkey = request.form.get('authkey')
    query = request.form.get('query')
    if not executors.get(alias):
        return Response(to_str(json.dumps({'success':False, 'errormessage':'Unknown alias'})), mimetype='text/json')
    id = start_hoffeye(alias, query, authkey)
    return Response(to_str(json.dumps({'success':True, 'id':id, 'errormessage':None})), mimetype='text/json')

@app.route("/hoffeye_remove", methods=['POST'])
def hoffeye_remove():
    alias = request.form.get('alias')
    id = request.form.get('id')
    remove_hoffeye(alias, id)
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/hoffeye_result", methods=['POST'])
def hoffeye_result():
    alias = request.form.get('alias')

    return Response(to_str(json.dumps(hoffeyes[alias].result())), mimetype='text/json')

@app.route("/hoffeye_stop", methods=['POST'])
def hoffeye_stop():
    alias = request.form.get('alias')
    stop_hoffeye(alias)
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/hoffeye_start", methods=['POST'])
def hoffeye_start():
    alias = request.form.get('alias')
    start_hoffeye(alias, None, None)
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')

@app.route("/list_snippets", methods=['POST'])
def list_snippets():
    column = request.form.get('alias')
    snippets = read_snippets_config()

    if snippets == None:
        snippets = {}

    return Response(to_str(json.dumps({'success':True, 'snippets': snippets })), mimetype='text/json')

@app.route("/set_snippet", methods=['POST'])
def set_snippet():
    id = request.form.get('id')
    column = request.form.get('column')
    name = request.form.get('name')
    sql = request.form.get('sql')

    if id == None or column == None or name == None or sql == None:
        raise Exception("id, column, name, sql must be set")


    snippets = read_snippets_config()

    if not column in snippets:
        snippets[column] = {}

    if not id in snippets:
        snippets[column][id] = {}

    snippets[column][id] = {
        'id': id,
        'column': column,
        'name': name,
        'sql': sql
    }

    write_snippets_config(snippets)

    return Response(to_str(json.dumps({'success':True })), mimetype='text/json')

@app.route("/hoffeye_clear", methods=['POST'])
def hoffeye_clear():
    alias = request.form.get('alias')
    clear_hoffeye(alias)
    return Response(to_str(json.dumps({'success':True, 'errormessage':None})), mimetype='text/json')


@app.route('/')
def site_main():
    return render_template('history.html')

#init server
main()
