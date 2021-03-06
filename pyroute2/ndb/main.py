'''
NDB
===

An experimental high-level RTNL management module.

.. warning::
    And it means really experimental.

Examples::

    from pyroute2 import NDB
    from pprint import pprint

    with NDB() as ndb:
        # ...
        for line ndb.routes.csv():
            print(line)
        # ...
        for record in ndb.interfaces.summary():
            print(record)
        # ...
        pprint(ndb.interfaces['eth0'])

        # ...
        pprint(ndb.interfaces[{'target': 'localhost',
                               'ifname': 'eth0'}])

        #
        # change object parameters
        #
        eth0 = ndb.interfaces['eth0']
        eth0['state'] = 'up'
        eth0.commit()

        #
        # create objects
        #
        test0 = ndb.interfaces.add(ifname='test0', kind='dummy')
        test0.commit()
        # ...
        test0.remove()
        test0.commit()

        #
        # it is mandatory to call ndb.close() or to use NDB
        # as a context manager
        #

Difference with IPDB
--------------------

NDB is designed to work with multiple event sources and with loads of
network objects.

Multiple sources::

    from pyroute2 import (NDB,
                          IPRoute,
                          NetNS,
                          RemoteIPRoute)

    sources = {'localhost': IPRoute(),
               'debian.test': RemoteIPRoute(protocol='ssh',
                                            hostname='192.168.122.54',
                                            username='netops'),
               'openbsd.test': RemoteIPRoute(protocol='ssh',
                                             hostname='192.168.122.60',
                                             username='netops'),
               'netns0': NetNS('netns0'),
               'docker': NetNS('/var/run/docker/netns/f2d2ba3e5987')}

    # NDB supports the context protocol, close() is called automatically
    with NDB(sources=sources) as ndb:
        # local interface
        print(ndb.interfaces[{'target': 'localhost',
                              'ifname': 'eth0'}])
        # remote interface
        print(ndb.interfaces[{'target': 'openbsd.test',
                              'ifname': 'ix0'}])
        # all the interfaces
        for i in ndb.interfaces.summary():
            print(i)

NDB stores all the data in an SQL database and creates objects on
demand. Statements like `ndb.interfaces['eth0']` create a new object
every time you run this statement. Thus::

    with NDB() as ndb:

        #
        # This will NOT work, as every line creates a new object
        #
        ndb.interfaces['eth0']['state'] = 'up'
        ndb.interfaces['eth0'].commit()

        #
        # This works
        #
        eth0 = ndb.interfaces['eth0']  # get the reference
        eth0['state'] = 'up'
        eth0.commit()

        #
        # The same with a context manager
        #
        with ndb.interfaces['eth0'] as eth0:
            eth0['state'] = 'up'
        # ---> <--- the context manager runs commit() at __exit__()


DB providers
------------

NDB supports different DB providers, now they are SQLite3 and PostgreSQL.
PostgreSQL access requires psycopg2 module::

    from pyroute2 import NDB

    # SQLite3 -- simple in-memory DB
    ndb = NDB(db_provider='sqlite3')

    # SQLite3 -- same as above
    ndb = NDB(db_provider='sqlite3',
              db_spec=':memory:')

    # SQLite3 -- file DB
    ndb = NDB(db_provider='sqlite3',
              db_spec='test.db')

    # PostgreSQL -- local DB
    ndb = NDB(db_provider='psycopg2',
              db_spec={'dbname': 'test'})

    # PostgreSQL -- remote DB
    ndb = NDB(db_provider='psycopg2',
              db_spec={'dbname': 'test',
                       'host': 'db1.example.com'})


'''
import gc
import json
import time
import atexit
import sqlite3
import logging
import weakref
import threading
import traceback
from functools import partial
from pyroute2 import config
from pyroute2 import IPRoute
from pyroute2.netlink.nlsocket import NetlinkMixin
from pyroute2.ndb import dbschema
from pyroute2.ndb.interface import (Interface,
                                    Bridge,
                                    Vlan)
from pyroute2.ndb.address import Address
from pyroute2.ndb.route import Route
from pyroute2.ndb.neighbour import Neighbour
from pyroute2.ndb.query import Query
from pyroute2.ndb.report import Report
try:
    import queue
except ImportError:
    import Queue as queue
try:
    import psycopg2
except ImportError:
    psycopg2 = None
log = logging.getLogger(__name__)


def target_adapter(value):
    #
    # MPLS target adapter for SQLite3
    #
    return json.dumps(value)


sqlite3.register_adapter(list, target_adapter)
SOURCE_FAIL_PAUSE = 5


class SyncStart(Exception):
    pass


class SchemaFlush(Exception):
    pass


class MarkFailed(Exception):
    pass


class DBMExitException(Exception):
    pass


class ShutdownException(Exception):
    pass


class InvalidateHandlerException(Exception):
    pass


class Factory(dict):
    '''
    The Factory() object returns RTNL objects on demand::

        ifobj1 = ndb.interfaces['eth0']
        ifobj2 = ndb.interfaces['eth0']
        # ifobj1 != ifobj2
    '''
    classes = {'interfaces': Interface,
               'vlan': Vlan,
               'bridge': Bridge,
               'addresses': Address,
               'routes': Route,
               'neighbours': Neighbour}

    def __init__(self, ndb, table, match_src=None, match_pairs=None):
        self.ndb = ndb
        self.table = table
        self.match_src = match_src
        self.match_pairs = match_pairs

    def get(self, key, table=None):
        return self.__getitem__(key, table)

    def add(self, **spec):
        spec['create'] = True
        return self[spec]
    add.__cptr__ = True

    def __getitem__(self, key, table=None):
        #
        # Construct a weakref handler for events.
        #
        # If the referent doesn't exist, raise the
        # exception to remove the handler from the
        # chain.
        #

        def wr_handler(wr, fname, *argv):
            try:
                return getattr(wr(), fname)(*argv)
            except:
                # check if the weakref became invalid
                if wr() is None:
                    raise InvalidateHandlerException()
                raise

        iclass = self.classes[table or self.table]
        ret = iclass(self,
                     key,
                     match_src=self.match_src,
                     match_pairs=self.match_pairs)
        wr = weakref.ref(ret)
        self.ndb._rtnl_objects.add(wr)
        for event, fname in ret.event_map.items():
            #
            # Do not trust the implicit scope and pass the
            # weakref explicitly via partial
            #
            (self
             .ndb
             .register_handler(event,
                               partial(wr_handler, wr, fname)))

        return ret

    def __setitem__(self, key, value):
        raise NotImplementedError()

    def __delitem__(self, key):
        raise NotImplementedError()

    def keys(self):
        raise NotImplementedError()

    def items(self):
        raise NotImplementedError()

    def values(self):
        raise NotImplementedError()

    def _dump(self, match=None):
        iclass = self.classes[self.table]
        cls = iclass.msg_class or self.ndb.schema.classes[iclass.table]
        keys = self.ndb.schema.compiled[iclass.view or iclass.table]['names']

        spec, values = self._match(match, cls, keys, iclass.table_alias)
        if iclass.dump and iclass.dump_header:
            yield iclass.dump_header
            with self.ndb.schema.db_lock:
                for record in (self
                               .ndb
                               .schema
                               .fetch(iclass.dump + spec, values)):
                    yield record
        else:
            yield ('target', 'tflags') + tuple([cls.nla2name(x) for x in keys])
            with self.ndb.schema.db_lock:
                for record in (self
                               .ndb
                               .schema
                               .fetch('SELECT * FROM %s AS %s %s'
                                      % (iclass.view or iclass.table,
                                         iclass.table_alias,
                                         spec),
                                      values)):
                    yield record

    def _csv(self, match=None, dump=None):
        if dump is None:
            dump = self._dump(match)
        for record in dump:
            row = []
            for field in record:
                if isinstance(field, int):
                    row.append('%i' % field)
                elif field is None:
                    row.append('')
                else:
                    row.append("'%s'" % field)
            yield ','.join(row)

    def _summary(self, match=None):
        iclass = self.classes[self.table]
        cls = iclass.msg_class or self.ndb.schema.classes[iclass.table]
        keys = self.ndb.schema.compiled[iclass.view or iclass.table]['names']

        spec, values = self._match(match, cls, keys, iclass.table_alias)
        if iclass.summary is not None:
            if iclass.summary_header is not None:
                yield iclass.summary_header
            for record in (self
                           .ndb
                           .schema
                           .fetch(iclass.summary + spec, values)):
                yield record
        else:
            header = tuple(['f_%s' % x for x in
                            ('target', ) +
                            self.ndb.schema.indices[iclass.table]])
            yield header
            key_fields = ','.join(header)
            for record in (self
                           .ndb
                           .schema
                           .fetch('SELECT %s FROM %s AS %s %s'
                                  % (key_fields,
                                     iclass.view or iclass.table,
                                     iclass.table_alias,
                                     spec), values)):
                yield record

    def _match(self, match, cls, keys, alias):
        values = []
        match = match or {}
        if self.match_src and self.match_pairs:
            for l_key, r_key in self.match_pairs.items():
                for src in self.match_src:
                    try:
                        match[l_key] = src[r_key]
                        break
                    except:
                        pass

        if match:
            spec = ' WHERE '
            conditions = []
            for key, value in match.items():
                keyc = []
                if cls.name2nla(key) in keys:
                    keyc.append(cls.name2nla(key))
                if key in keys:
                    keyc.append(key)
                if not keyc:
                    raise KeyError('key %s not found' % key)
                if len(keyc) == 1:
                    conditions.append('%s.f_%s = %s' % (alias, keyc[0],
                                                        self.ndb.schema.plch))
                    values.append(value)
                elif len(keyc) == 2:
                    conditions.append('(%s.f_%s = %s OR %s.f_%s = %s)'
                                      % (alias, keyc[0], self.ndb.schema.plch,
                                         alias, keyc[1], self.ndb.schema.plch))
                    values.append(value)
                    values.append(value)
            spec = ' WHERE %s' % ' AND '.join(conditions)
        else:
            spec = ''
        return spec, values

    def csv(self, *argv, **kwarg):
        return Report(self._csv(*argv, **kwarg))

    def dump(self, *argv, **kwarg):
        fmt = kwarg.pop('fmt', 'native')
        if fmt == 'native':
            return Report(self._dump(*argv, **kwarg))
        elif fmt == 'csv':
            return Report(self._csv(dump=self._dump(*argv, **kwarg)))
        else:
            raise ValueError('format not supported')

    def summary(self, *argv, **kwarg):
        fmt = kwarg.pop('fmt', 'native')
        if fmt == 'native':
            return Report(self._summary(*argv, **kwarg))
        elif fmt == 'csv':
            return Report(self._csv(dump=self._summary(*argv, **kwarg)))
        else:
            raise ValueError('format not supported')


class Source(object):
    '''
    The RNTL source. The source that is used to init the object
    must comply to IPRoute API, must support the async_cache. If
    the source starts additional threads, they must be joined
    in the source.close()
    '''

    def __init__(self, evq, target, source,
                 event=None,
                 persistent=True,
                 **nl_kwarg):
        self.th = None
        self.nl = None
        # the event queue to send events to
        self.evq = evq
        # the target id -- just in case
        self.target = target
        # RTNL API
        self.nl_prime = source
        self.nl_kwarg = nl_kwarg
        #
        self.event = event
        self.shutdown = threading.Event()
        self.started = threading.Event()
        self.lock = threading.Lock()
        self.started.clear()
        self.persistent = persistent
        self.state = 'init'

    def __repr__(self):
        if isinstance(self.nl_prime, NetlinkMixin):
            name = self.nl_prime.__class__.__name__
        elif isinstance(self.nl_prime, type):
            name = self.nl_prime.__name__

        return '[%s] <%s %s>' % (self.state, name, self.nl_kwarg)

    def start(self):

        #
        # The source thread routine -- get events from the
        # channel and forward them into the common event queue
        #
        # The routine exists on an event with error code == 104
        #
        def t(self):
            while True:
                if self.nl is not None:
                    try:
                        self.nl.close(err=0)
                    except Exception as e:
                        log.warning('[%s] source restart: %s'
                                    % (self.target, e))
                try:
                    self.state = 'connecting'
                    if isinstance(self.nl_prime, NetlinkMixin):
                        self.nl = self.nl_prime
                    elif isinstance(self.nl_prime, type):
                        self.nl = self.nl_prime(**self.nl_kwarg)
                    else:
                        raise TypeError('source channel not supported')
                    self.state = 'loading'
                    #
                    self.nl.bind(async_cache=True, clone_socket=True)
                    #
                    # Initial load -- enqueue the data
                    #
                    self.evq.put((self.target, (SchemaFlush(), )))
                    self.evq.put((self.target, self.nl.get_links()))
                    self.evq.put((self.target, self.nl.get_addr()))
                    self.evq.put((self.target, self.nl.get_neighbours()))
                    self.evq.put((self.target, self.nl.get_routes()))
                    self.started.set()
                    self.shutdown.clear()
                    self.state = 'running'
                    if self.event is not None:
                        self.evq.put((self.target, (self.event, )))
                    while True:
                        try:
                            msg = tuple(self.nl.get())
                        except Exception as e:
                            log.error('[%s] source error: %s' %
                                      (self.target, e))
                            msg = None
                        if msg is None or \
                                msg[0]['header']['error'] and \
                                msg[0]['header']['error'].code == 104:
                            self.state = 'stopped'
                            # thus we make sure that all the events from
                            # this source are consumed by the main loop
                            # in __dbm__() routine
                            sync = threading.Event()
                            self.evq.put((self.target, (sync, )))
                            sync.wait()
                            return
                        self.evq.put((self.target, msg))
                except TypeError:
                    raise
                except Exception as e:
                    self.started.set()
                    self.state = 'failed'
                    log.error('[%s] source error: %s' % (self.target, e))
                    self.evq.put((self.target, (MarkFailed(), )))
                    if self.persistent:
                        log.debug('[%s] sleeping before restart' % self.target)
                        self.shutdown.wait(SOURCE_FAIL_PAUSE)
                        if self.shutdown.is_set():
                            log.debug('[%s] source shutdown' % self.target)
                            return
                    else:
                        return

        #
        # Start source thread
        with self.lock:
            if (self.th is not None) and self.th.is_alive():
                raise RuntimeError('source is running')

            self.th = (threading
                       .Thread(target=t, args=(self, ),
                               name='NDB event source: %s' % (self.target)))
            self.th.start()

    def close(self):
        with self.lock:
            if self.nl is not None:
                try:
                    self.nl.close()
                except Exception as e:
                    log.error('[%s] source close: %s' % (self.target, e))
            if self.th is not None:
                self.th.join()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class NDB(object):

    def __init__(self,
                 sources=None,
                 db_provider='sqlite3',
                 db_spec=':memory:',
                 rtnl_log=False):

        self.ctime = self.gctime = time.time()
        self.schema = None
        self._debug = None
        self._db = None
        self._dbm_thread = None
        self._dbm_ready = threading.Event()
        self._global_lock = threading.Lock()
        self._event_map = None
        self._event_queue = queue.Queue(maxsize=100)
        #
        # fix sources prime
        if sources is None:
            self._nl = {'localhost': {'class': IPRoute}}
        elif isinstance(sources, NetlinkMixin):
            self._nl = {'localhost': sources}
        elif isinstance(sources, dict):
            self._nl = sources

        self.sources = {}
        self._db_provider = db_provider
        self._db_spec = db_spec
        self._db_rtnl_log = rtnl_log
        atexit.register(self.close)
        self._rtnl_objects = set()
        self._dbm_ready.clear()
        self._dbm_thread = threading.Thread(target=self.__dbm__,
                                            name='NDB main loop')
        self._dbm_thread.start()
        self._dbm_ready.wait()
        self.interfaces = Factory(self, 'interfaces')
        self.addresses = Factory(self, 'addresses')
        self.routes = Factory(self, 'routes')
        self.neighbours = Factory(self, 'neighbours')
        self.vlans = Factory(self, 'vlan')
        self.bridges = Factory(self, 'bridge')
        self.query = Query(self.schema)

    def _get_view(self, name, match_src=None, match_pairs=None):
        return Factory(self, name, match_src, match_pairs)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def show(self, *argv, **kwarg):
        ptr = self
        for word in argv:
            ptr = getattr(ptr, word)
        if hasattr(ptr, '__call__'):
            return ptr(**kwarg)
        else:
            return ptr

    def debug(self, mode=None):
        if mode is None:
            return self._debug is not None
        elif mode == 'on' and self._debug is None:
            self._debug = {'logger': logging.getLogger(''),
                           'handler': logging.StreamHandler()}
            (self
             ._debug['logger']
             .addHandler(self._debug['handler']))
            (self
             ._debug['logger']
             .setLevel(logging.DEBUG))
        elif mode == 'off' and self._debug is not None:
            (self
             ._debug['logger']
             .setLevel(logging.INFO))
            (self
             ._debug['logger']
             .removeHandler(self._debug['handler']))

    def register_handler(self, event, handler):
        if event not in self._event_map:
            self._event_map[event] = []
        self._event_map[event].append(handler)

    def unregister_handler(self, event, handler):
        self._event_map[event].remove(handler)

    def execute(self, *argv, **kwarg):
        return self.schema.execute(*argv, **kwarg)

    def wait(self, spec):
        '''
        Example::

            ndb.wait({'interfaces': [{'ifname': 'eth0'}],
                      'addresses': [{'address': '10.0.0.1',
                                     'prefixlen': 24},
                                    {'address': '10.0.0.2',
                                     'prefixlen': 24}]})
        '''
        if not isinstance(spec, dict):
            raise ValueError('wrong spec type, must be dict')

        # install a limited events queue -- for a possible immediate reaction
        evq = queue.Queue(maxsize=512)

        def handler(evq, target, event):
            # ignore the "queue full" exception
            #
            # if we miss some events here, nothing bad happens: we just
            # load them from the DB after a timeout, falling back to
            # the DB polling
            #
            # the most important here is not to allocate too much memory
            try:
                evq.put_nowait((target, event))
            except queue.Full:
                pass

        #
        hdl = partial(handler, evq)
        for event in spec:
            self.register_handler(self.schema.classes[event], hdl)

        #
        wait_for = []
        for event, objs in spec.items():
            for obj in objs:
                wait_for.append((event, self.schema.classes[event], obj))

        #
        def check_db(l):
            for event, evc, obj in tuple(l):
                target = obj.get('target', 'localhost')
                source = self.sources[target]
                if source.state != 'running':
                    raise RuntimeError('rtnl source not available')
                try:
                    getattr(self, event)[obj]
                except KeyError:
                    continue
                l.remove((event, evc, obj))

        #
        check_db(wait_for)

        #
        while wait_for:
            try:
                target, msg = evq.get(timeout=1)
            except queue.Empty:
                continue
            finally:
                check_db(wait_for)

            #
            for event, evc, obj in tuple(wait_for):
                if evc != type(msg):
                    continue
                for key, value in obj.items():
                    if key == 'target' and value != target:
                        break
                    elif value not in (msg.get(key),
                                       msg.get_attr(msg.name2nla(key))):
                        break
                else:
                    wait_for.remove((event, evc, obj))

        #
        for event in spec:
            self.unregister_handler(self.schema.classes[event], hdl)

        del evq
        del hdl
        del wait_for
        gc.collect()

    def close(self):
        with self._global_lock:
            if hasattr(atexit, 'unregister'):
                atexit.unregister(self.close)
            else:
                try:
                    atexit._exithandlers.remove((self.close, (), {}))
                except ValueError:
                    pass
            if self.schema:
                # release all the failed sources waiting for restart
                self._event_queue.put(('localhost', (ShutdownException(), )))
                # release all the sources
                for target, source in self.sources.items():
                    source.close()
                # shutdown the _dbm_thread
                self._event_queue.put(('localhost', (DBMExitException(), )))
                self._dbm_thread.join()
                # close the database
                self.schema.commit()
                self.schema.close()

    def __initdb__(self):
        with self._global_lock:
            #
            # close the current db, if opened
            if self.schema:
                self.schema.commit()
                self.schema.close()
            #
            # ACHTUNG!
            # check_same_thread=False
            #
            # Please be very careful with the DB locks!
            #
            if self._db_provider == 'sqlite3':
                self._db = sqlite3.connect(self._db_spec,
                                           check_same_thread=False)
            elif self._db_provider == 'psycopg2':
                self._db = psycopg2.connect(**self._db_spec)

            if self.schema:
                self.schema.db = self._db

    def disconnect_source(self, target, flush=True):
        '''
        Disconnect an event source from the DB. Raise KeyError if
        there is no such source.

        :param target: node name or UUID
        '''
        # close the source
        self.sources[target].close()
        del self.sources[target]
        #
        if flush:
            self.schema.flush(target)

    def connect_source(self, target, source, event=None):
        '''
        Connect an event source to the DB. All arguments are required.

        :param target: node name or UUID, any hashable value
        :param nl: an IPRoute object to init Source() class
        :param event: an optional Event() to send in the end

        The source connection is an async process so there should be
        a way to wain until it is registered. One can provide an Event()
        that will be set by the main NDB loop when the source is
        connected.
        '''
        #
        # flush the DB
        self.schema.flush(target)
        #
        # register the channel
        if target in self.sources:
            self.disconnect_source(target)
        try:
            if isinstance(source, NetlinkMixin):
                self.sources[target] = Source(self._event_queue,
                                              target, source, event)
            elif isinstance(source, dict):
                iclass = source.pop('class')
                persistent = source.pop('persistent', False)
                self.sources[target] = Source(self._event_queue,
                                              target, iclass, event,
                                              persistent, **source)
            elif isinstance(source, Source):
                self.sources[target] = Source
            else:
                raise TypeError('source not supported')

            self.sources[target].start()
        except:
            if target in self.sources:
                self.sources[target].close()
                del self.sources[target]
            self.schema.flush(target)
            raise

    def __dbm__(self):

        def default_handler(target, event):
            if isinstance(event, Exception):
                raise event
            logging.warning('unsupported event ignored: %s' % type(event))

        def check_sources_started(self, _locals, target, event):
            _locals['countdown'] -= 1
            if _locals['countdown'] == 0:
                self._dbm_ready.set()

        _locals = {'countdown': len(self._nl)}

        # init the events map
        event_map = {type(self._dbm_ready): [lambda t, x: x.set()],
                     SchemaFlush: [lambda t, x: self.schema.flush(t)],
                     MarkFailed: [lambda t, x: self.schema.mark(t, 1)],
                     SyncStart: [partial(check_sources_started,
                                         self, _locals)]}
        self._event_map = event_map

        event_queue = self._event_queue

        self.__initdb__()
        self.schema = dbschema.init(self._db,
                                    self._db_provider,
                                    self._db_rtnl_log,
                                    id(threading.current_thread()))
        for target, source in self._nl.items():
            try:
                self.connect_source(target, source, SyncStart())
            except Exception as e:
                log.error('could not connect source %s: %s' % (target, e))

        for (event, handlers) in self.schema.event_map.items():
            for handler in handlers:
                self.register_handler(event, handler)

        while True:
            target, events = event_queue.get()
            for event in events:
                handlers = event_map.get(event.__class__, [default_handler, ])
                for handler in tuple(handlers):
                    try:
                        handler(target, event)
                    except InvalidateHandlerException:
                        try:
                            handlers.remove(handler)
                        except:
                            log.error('could not invalidate event handler:\n%s'
                                      % traceback.format_exc())
                    except ShutdownException:
                        for target, source in self.sources.items():
                            source.shutdown.set()
                    except DBMExitException:
                        return
                    except:
                        log.error('could not load event:\n%s\n%s'
                                  % (event, traceback.format_exc()))
                if time.time() - self.gctime > config.gc_timeout:
                    self.gctime = time.time()
                    for wr in tuple(self._rtnl_objects):
                        if wr() is None:
                            self._rtnl_objects.remove(wr)
