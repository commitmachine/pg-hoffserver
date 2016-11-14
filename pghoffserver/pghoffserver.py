from __future__ import unicode_literals, print_function
import sys, os, uuid, datetime, time, psycopg2, sqlparse, sqlite3, re, zerorpc
import simplejson as json
from threading import Lock, Thread
from multiprocessing import Queue
from collections import defaultdict
from pgcli.pgexecute import PGExecute
from pgspecial import PGSpecial
from pgcli.completion_refresher import CompletionRefresher
from prompt_toolkit.document import Document
import logging
logging.basicConfig()
try:
    from urlparse import urlparse
except ImportError:
    from urllib.parse import urlparse
special = PGSpecial()
from psycopg2.extensions import (TRANSACTION_STATUS_IDLE,
                                TRANSACTION_STATUS_ACTIVE,
                                TRANSACTION_STATUS_INTRANS,
                                TRANSACTION_STATUS_INERROR,
                                TRANSACTION_STATUS_UNKNOWN)
home_dir = os.path.expanduser('~/.pghoffserver')
completers = defaultdict(list)  # Dict mapping urls to pgcompleter objects
completer_lock = Lock()
executors = defaultdict(list)  # Dict mapping buffer ids to pgexecutor objects
executor_lock = Lock()
bufferConnections = defaultdict(str) #Dict mapping bufferids to connectionstrings
queryResults = defaultdict(list)
dbSyncQueue = Queue()
type_dict = defaultdict(dict)
config = {}
serverList = {}
uuids_pending_execution = []
executor_queues = defaultdict(lambda: Queue())
db_name = 'hoff.db'

def main():
    global serverList
    global config
    global apikey
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
            config = json.load(json_data_file)
            #Todo: load PGCLI using site-dirs from config file.
            serverList = config['connections']
    except Exception:
        config = dict()
        serverList = dict()
    init_db()
    s = zerorpc.Server(RPC(), heartbeat=1000)
    s.bind("tcp://127.0.0.1:4242")
    s.run()

def init_db():
    sql = """CREATE TABLE IF NOT EXISTS QueryData(
      alias text, batchid text, queryid text, dynamic_table_name text, columns text, rows text,
      query text, notices text, statusmessage text,
      runtime_seconds int, error text,
      datestamp timestamp
    )"""
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.execute(sql)
    conn.close()
    t = Thread(target=db_worker,
                   name='db_worker')
    t.setDaemon(True)
    t.start()

def db_worker():
    conn = sqlite3.connect(home_dir + '/' + db_name)
    while True:
        result = dbSyncQueue.get(block=True)
        for r in result:
            conn.cursor().execute("INSERT INTO QueryData VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (r['alias'], r['batchid'], r['queryid'], None, json.dumps(r['columns']), json.dumps(r['rows'], default=str),
                r['query'], json.dumps(r['notices']),
                r['statusmessage'], r['runtime_seconds'], r['error'], r['timestamp']))
            conn.commit()

def to_str(string):
    if sys.version_info < (3,0):
         return unicode(string)
    return str(string)

def new_server(alias, url, requiresauthkey):
    serverList[alias] = {'url':url, 'requiresauthkey':requiresauthkey}
    config['connections'] = serverList
    with open(home_dir + '/config.json', mode='w') as configfile:
        json.dump(config, configfile)

def remove_server(alias):
    if config['connections'].get(alias):
        del config['connections'][alias]
    if serverList.get(alias):
        del serverList[alias]
    with open(home_dir + 'config.json', mode='w', encoding='utf-8') as configfile:
        json.dump(config, configfile)

def connect_server(alias, authkey=None):
    settings = {
        'generate_aliases' : True,
        'casing_file' : os.path.expanduser('~/.config/pgcli/casing'),
        'generate_casing_file' : True,
        'single_connection': True
    }
    server = serverList.get(alias, None)
    if not server:
        return {'alias': alias, 'success':False, 'errormessage':'Unknown alias.'}
    if executors[alias]:
        return {'alias': alias, 'success':False, 'errormessage':'Already connected to server.'}
    refresher = CompletionRefresher()
    try:
        with executor_lock:
            executor = new_executor(server['url'], authkey)
            with executor.conn.cursor() as cur:
                cur.execute('SELECT oid, oid::regtype::text FROM pg_type')
                type_dict[alias] = dict(row for row in cur.fetchall())
            executors[alias] = executor
            refresher.refresh(executor, special=special, callbacks=(
                                lambda c: swap_completer(c, alias)), settings=settings)
            serverList[alias]['connected'] = True
    except psycopg2.Error as e:
        return {'success':False, 'errormessage':to_str(e)}

    #create a queue for this alias and start a worker thread
    executor_queues[alias] = Queue()
    t = Thread(target=executor_queue_worker,
                   args=(alias,),
                   name='executor_queue_worker')
    t.setDaemon(True)
    t.start()

    return {'alias': alias, 'success':True, 'errormessage':None}

def refresh_servers():
    with executor_lock:
        for alias, server in serverList.items():
            if alias in executors:
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

def server_status(alias):
    with executor_lock:
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
        executors[alias].conn.cancel()
        executors[alias].conn.rollback()
    return {'success':True, 'errormessage':None}

def new_executor(url, pwd=None, settings=None):
    uri = urlparse(url)
    database = uri.path[1:]  # ignore the leading fwd slash
    dsn = None  # todo: what is this for again
    return PGExecute(database, uri.username, pwd or uri.password, uri.hostname, uri.port, dsn)

def swap_completer(comp,alias):
    completers[alias] = comp

def get_transaction_status_text(status):
    return {
        TRANSACTION_STATUS_IDLE: 'idle',
        TRANSACTION_STATUS_ACTIVE: 'active',
        TRANSACTION_STATUS_INTRANS: 'intrans',
        TRANSACTION_STATUS_INERROR: 'inerror',
        TRANSACTION_STATUS_UNKNOWN: 'unknown'
    }[status]

def queue_query(uuid, alias, sql):
    executor_queues[alias].put({'sql': sql, 'uuid': uuid})

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
        query = executor_queues[alias].get(block=True)
        sql = query['sql']
        uid = query['uuid']

        for sql in sqlparse.split(sql):
            queryResults[uid].append({
                'alias': alias,
                'batchid': uid,
                'queryid': to_str(uuid.uuid1()),
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
            })
        with executor.conn.cursor() as cur:
            for n, qr in enumerate(queryResults[uid]):
                timestamp_ts = time.mktime(datetime.datetime.now().timetuple())
                currentQuery = queryResults[uid][n]
                currentQuery['timestamp'] = time.strftime('%Y-%m-%d %H:%M:%S')
                currentQuery['executing'] = True
                queryResults[uid][n] = currentQuery
                #Check if there are any dynamic tables in the query
                query = update_query_with_dynamic_tables(qr['query'])
                #run query
                try:
                    cur.execute(query)
                except psycopg2.Error as e:
                    currentQuery['error'] = to_str(e)
                if cur.description:
                    currentQuery['columns'] = [{'name': d.name, 'type_code': d.type_code,
                                                'type': type_dict[alias][d.type_code]} for d in cur.description]
                    currentQuery['rows'] = list(cur.fetchall())
                #update query result
                currentQuery['runtime_seconds'] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                currentQuery['complete'] = True
                currentQuery['executing'] = False
                currentQuery['statusmessage'] = cur.statusmessage
                notices = []
                while executor.conn.notices:
                    notices.append(executor.conn.notices.pop(0))
                currentQuery['notices'] = notices
                queryResults[uid][n] = currentQuery
            uuids_pending_execution.remove(uid)

def update_query_with_dynamic_tables(query):
    dynamic_tables = list_dynamic_tables()
    if not dynamic_tables:
        return query
    for x in dynamic_tables:
        if '##' + x['dynamic_table_name'] in query:
            query = query.replace('##' + x['dynamic_table_name'], construct_dynamic_table(x['dynamic_table_name']))
    return query

def get_word(text, position):
    #print(text, file=sys.stderr)
    #print(position, file=sys.stderr)
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

def fetch_result(uuid):
    result = queryResults[uuid]
    if not result: #look in uuids_pending_execution list and wait up to 5 seconds
        if uuid in uuids_pending_execution:
            for x in range(1,500):
                time.sleep(0.01)
                result = queryResults[uuid]
                if result:
                    break
    if not result: #look for result in db
        conn = sqlite3.connect(home_dir + '/' + db_name)
        conn.row_factory = dict_factory
        cur = conn.cursor()
        cur.execute("SELECT * FROM QueryData WHERE batchid = ?", (to_str(uuid),))
        row = cur.fetchone() ##todo fetch whole batch of queries
        if row:
            result = {
                'alias': row["alias"],
                'batchid': row['batchid'],
                'queryid': row['queryid'],
                'columns': json.loads(row["columns"]),
                'rows': json.loads(row["rows"]),
                'query': row["query"],
                'notices': json.loads(row["notices"]),
                'statusmessage': row["statusmessage"],
                'complete': True,
                'executing': False,
                'timestamp': row["datestamp"],
                'runtime_seconds': row["runtime_seconds"],
                'error': row["error"]
            }
            return to_str(json.dumps(result))
        else:
            return to_str(json.dumps({'success':False, 'errormessage':'Unknown uuid.'}))
    try:
        sync_to_db = True
        for r in result:
            if r['executing'] == True:
                sync_to_db = False
                timestamp_ts = time.mktime(datetime.datetime.strptime(r["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
                r["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
            r['transaction_status'] = get_transaction_status_text(executors[r['alias']].conn.get_transaction_status())
        if sync_to_db: #put result in queue for db-storage
            dbSyncQueue.put(result)
            del queryResults[uuid]
        return to_str(json.dumps(result, default=str))
    except Exception as e:
        return to_str(json.dumps({'success':False, 'errormessage':'Not connected.', 'actual_error' : str(e)}))

def create_dynamic_table(queryid, name):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.cursor().execute('UPDATE QueryData SET dynamic_table_name = ? WHERE queryid = ?;', (name, to_str(queryid)))
    conn.commit()
    conn.close()
    return to_str(json.dumps({'success':True, 'errormessage':None})

def delete_dynamic_table(uuid = None, alias = None):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    if uuid:
        where_sql = ' WHERE uuid = ?;'
        param = uuid
    elif alias:
        where_sql = 'WHERE alias = ?;'
        param = alias
    else:
        where_sql = ';'
    conn.cursor().execute('UPDATE QueryData SET dynamic_table_name = NULL' + where_sql, (param,))
    conn.commit()
    conn.close()
    return to_str(json.dumps({'success':True, 'errormessage':None}))

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
        output.append(",".join( (to_str(column) if column else to_str('NULL')) if header['type'] in ('integer', 'bigint', 'numeric', 'smallint') else ("'" + to_str(column) + "'" if column else to_str('NULL')) for column, header in zip(row, columnheaders)))
    sql += "),(".join(str(column) for column in output)
    sql = '(SELECT * FROM (VALUES(' + sql + ')) DT (' + ",".join(str(column['name']) for column in columnheaders) + '))'
    return sql

def search_query_history(q, search_data=False):
    conn = sqlite3.connect(home_dir + '/' + db_name)
    conn.row_factory = dict_factory
    cur = conn.cursor()
    cur.execute("""SELECT alias,
        CASE WHEN LENGTH(query) > 50 THEN substr(query, 0, 50) || '...' ELSE query END as query,
        runtime_seconds, datestamp as timestamp, batchid, queryid FROM QueryData WHERE query LIKE :q """
        + (" OR rows LIKE :q" if search_data else "")
        + " ORDER BY datestamp DESC;", ({"q":'%' + q + '%'}))
    result = cur.fetchall()
    return result

def get_meta_data(alias, name):
    comps = completers[alias].get_completions(
                Document(text='select * from bank', cursor_position=18), None)
    print(comps, file=sys.stderr)

class RPC(object):
    def query(self, alias, sql):
        uid = to_str(uuid.uuid1())
        sstatus = server_status(alias)
        if not sstatus['success']:
            return to_str(json.dumps(sstatus))
        uuids_pending_execution.append(uid)
        queue_query(uid, alias, sql)
        return to_str(json.dumps({'success':True, 'guid':uid, 'Url':'localhost:5000/result/' + uid, 'errormessage':None}))

    def result(self, uuid):
        return fetch_result(uuid)

    def executing():
        output = []
        uuid_delete = []
        for n, uuid in enumerate(queryResults):
            sync_to_db = True
            for n, r in enumerate(queryResults[uuid]):
                if r['executing']:
                    sync_to_db = False
                    timestamp_ts = time.mktime(datetime.datetime.strptime(r["timestamp"], '%Y-%m-%d %H:%M:%S').timetuple())
                    r["runtime_seconds"] = int(time.mktime(datetime.datetime.now().timetuple())-timestamp_ts)
                r['transaction_status'] = get_transaction_status_text(executors[r['alias']].conn.get_transaction_status())
                output.append(r)
            if sync_to_db:
                dbSyncQueue.put(queryResults[uuid])
                uuid_delete.append(uuid)
        for uuid in uuid_delete:
            del queryResults[uuid]
        return to_str(json.dumps(output))

    def completions(self, pos, query, alias):
        dynamic_tables_match = find_dynamic_table(query, pos)
        dt_out = []
        if alias in completers:
            if dynamic_tables_match:
                dt_out = [{'text': c, 'type': 'Dynamic table'} for c in dynamic_tables_match]
            comps = completers[alias].get_completions(
                        Document(text=query, cursor_position=int(pos)), None)
            comps_out = [{'text': c.text, 'type': c._display_meta} for c in comps]
            out = dt_out + comps_out
            return to_str(json.dumps(out))
        return to_str(json.dumps({'success':False, 'errormessage':'Not connected to server.'}))

    def list_servers(self):
        refresh_servers()
        return to_str(json.dumps(serverList))

    def connect(self, alias, authkey=None):
        return to_str(json.dumps(connect_server(alias, authkey)))

    def addserver(self, alias, url):
        if next((s for (a, s) in serverList.items() if a == alias), None):
            return to_str(json.dumps({'success':False, 'errormessage':'Server alias already exists.'}))
        else:
            requiresauthkey = request.form['requiresauthkey']
            new_server(alias, url, requiresauthkey)
            return to_str(json.dumps({'success':True, 'errormessage':None}))

    def delserver(self, alias):
        try:
            remove_server(alias)
            return to_str(json.dumps({'success':True, 'errormessage':None}))
        except Exception as e:
            return to_str(json.dumps({'success':False, 'errormessage':to_str(e)}))

    def disconnect(self, alias):
        try:
            disconnect_server(alias)
            return to_str(json.dumps({'success':True, 'errormessage':None}))
        except Exception as e:
            return to_str(json.dumps({'success':False, 'errormessage':to_str(e)}))

    def cancel(self, alias):
        try:
            cancel_execution(alias)
            return to_str(json.dumps({'success':True, 'errormessage':None}))
        except Exception as e:
            return to_str(json.dumps({'success':False, 'errormessage':to_str(e)}))

    def create_dynamic_table(self, queryid, name):
        return create_dynamic_table(queryid, name)

    def delete_dynamic_table(self, uuid):
        return delete_dynamic_table(uuid, None)

    def delete_dynamic_tables(self, alias):
        return delete_dynamic_table(None, alias)

    def list_dynamic_tables(self, alias):
        dynamic_tables =  list_dynamic_tables(alias)
        if dynamic_tables:
            return to_str(json.dumps(dynamic_tables))
        else:
            return to_str(json.dumps(None))

    def export_dynamic_table(self, name):
        return to_str(construct_dynamic_table(name))

    def search_history(self, q, serch_data=False):
        result = search_query_history(q, search_data)
        if not result:
            return to_str(json.dumps({'success':False, 'errormessage':'No queries match the given search criteria.'}))
        return to_str((json.dumps(result)))

    def get_meta_data(self, alias, name):
        if alias not in serverList:
            return to_str(json.dumps({'success':False, 'errormessage':'Unknown alias.'}))
        if alias not in executors:
            return to_str(json.dumps({'success':False, 'errormessage':'Not connected.'}))
        if not name:
            return to_str(json.dumps({'success':False, 'errormessage':'No object specified.'}))
        return to_str(get_meta_data(alias, name))

if __name__ == "__main__":
    main()
