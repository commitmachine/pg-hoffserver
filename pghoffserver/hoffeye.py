import time, sys, random, string
from collections import defaultdict
from threading import Thread
import copy

def to_str(string):
    if sys.version_info < (3,0):
         return unicode(string)
    return str(string)

#these queries go outside of the regular flow and therefor don't get logged
class HoffEye:

    #dict containing all queries watched by each HoffEye instance
    watchedQueries = []
    type_dict = defaultdict()
    executor = None
    queryDelay = None
    watching = False
    t = Thread()

    def __init__(self, executor, queryDelay=None):
        #initialte HoffEye, start watcher thread
        self.queryDelay = queryDelay or 1
        self.executor = executor

        #fetch datatypes before doing anything else
        with self.executor.conn.cursor() as cur:
            cur.execute('SELECT oid, oid::regtype::text FROM pg_type')
            self.type_dict = dict(row for row in cur.fetchall())

    def status(self):
        return {'watching': self.watching }

    def stop(self):
        self.watching = False

    def clear(self):
        self.watchedQueries = defaultdict()

    def start(self):
        if not self.watching:
            self.watching = True
            self.t = Thread(target=self.watch_queries,
                           name='watch')
            self.t.setDaemon(True)
            self.t.start()

    def remove_watch(self, id):
        self.watchedQueries = [a for a in self.watchedQueries if a['id'] != id]

    def add_watch(self, query):
        id = ''.join(random.choice(string.ascii_uppercase + string.ascii_lowercase) for _ in range(20))
        self.watchedQueries.append(
            {
                'sql' : query,
                'id' : id,
                'result': {
                        'columns': None,
                        'rows': None,
                        'notices': None,
                        'statusmessage': None,
                        'new_data': False
                }
            }
        )
        return id

    def watch_queries(self):
        while self.watching:
            for query in self.watchedQueries:
                with self.executor.conn.cursor() as cur:
                    cur.execute(query['sql'])
                    if cur.description:
                        columns = [
                            {
                                'name': d.name,
                                'type_code': d.type_code,
                                'type': self.type_dict[d.type_code],
                                'field':d.name + str(i),
                                'data_length': 0
                            } for i, d in enumerate(cur.description, 1)
                        ]
                        query['result']['columns'] = columns
                        old_rows = query['result']['rows']
                        query['result']['rows'] = []
                        for row in cur.fetchall():
                            rowdict = {}
                            query['result']['rows'].append(rowdict)
                            for col, data in zip(columns, row):
                                rowdict[col["field"]] = data
                                col['data_length'] = max(len(to_str(data)), col['data_length'])
                    #update query result
                    query['result']['statusmessage'] = cur.statusmessage
                    notices = []
                    while self.executor.conn.notices:
                        notices.append(self.executor.conn.notices.pop(0))
                    query['result']['notices'] = notices
                    if query['result']['rows'] != old_rows:
                        query['result']['new_data'] = True
            time.sleep(self.queryDelay)

    def result(self):
        result = copy.deepcopy(self.watchedQueries)
        if len([x for x in self.watchedQueries if x['result']['new_data']]) > 0:
            for query in self.watchedQueries:
                query['result']['new_data'] = False
            return result
