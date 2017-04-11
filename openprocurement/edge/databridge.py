# -*- coding: utf-8 -*-
from gevent import monkey
monkey.patch_all()

import math
import logging
import logging.config
import os
import psutil
import argparse
import uuid
from couchdb import Server, Session
from httplib import IncompleteRead
from yaml import load
from urlparse import urlparse
from openprocurement_client.sync import ResourceFeeder, FINISHED
from openprocurement_client.exceptions import RequestFailed
from openprocurement_client.client import TendersClient as APIClient
from openprocurement.edge.utils import (
    prepare_couchdb,
    prepare_couchdb_views,
    DataBridgeConfigError
)
import gevent.pool
from gevent import spawn, sleep
from gevent.queue import Queue, Empty
from datetime import datetime, timedelta
from .workers import ResourceItemWorker
from .utils import clear_api_client_queue

try:
    import urllib3.contrib.pyopenssl
    urllib3.contrib.pyopenssl.inject_into_urllib3()
except ImportError:
    pass

logger = logging.getLogger(__name__)

WORKER_CONFIG = {
    'resource': 'tenders',
    'client_inc_step_timeout': 0.1,
    'client_dec_step_timeout': 0.02,
    'drop_threshold_client_cookies': 2,
    'worker_sleep': 5,
    'retry_default_timeout': 3,
    'retries_count': 10,
    'queue_timeout': 3,
    'bulk_save_limit': 1000,
    'bulk_save_interval': 5
}

DEFAULTS = {
    'retrieve_mode': '_all_',
    'workers_inc_threshold': 75,
    'workers_dec_threshold': 35,
    'workers_min': 1,
    'workers_max': 3,
    'workers_pool': 3,
    'retry_workers_min': 1,
    'retry_workers_max': 2,
    'retry_workers_pool': 2,
    'retry_resource_items_queue_size': -1,
    'filter_workers_count': 1,
    'watch_interval': 10,
    'user_agent': 'edge.multi',
    'log_db_name': 'logs_db',
    'resource_items_queue_size': 10000,
    'input_queue_size': 10000,
    'resource_items_limit': 1000,
    'queues_controller_timeout': 60,
    'filter_workers_pool': 1,
    'bulk_query_interval': 5,
    'bulk_query_limit': 1000,
    'couch_url': 'http://127.0.0.1:5984',
    'db_name': 'edge_db',
    'perfomance_window': 300
}


class EdgeDataBridge(object):

    """Edge Bridge"""

    def __init__(self, config):
        super(EdgeDataBridge, self).__init__()
        self.config = config
        self.workers_config = {}
        self.bridge_id = uuid.uuid4().hex
        self.api_host = self.config_get('resources_api_server')
        self.api_version = self.config_get('resources_api_version')
        self.retrievers_params = self.config_get('retrievers_params')

        # Check up_wait_sleep
        up_wait_sleep = self.retrievers_params.get('up_wait_sleep')
        if up_wait_sleep is not None and up_wait_sleep < 30:
            raise DataBridgeConfigError('Invalid \'up_wait_sleep\' in '
                                        '\'retrievers_params\'. Value must be '
                                        'grater than 30.')

        # Workers settings
        for key in WORKER_CONFIG:
            self.workers_config[key] = (self.config_get(key) or
                                        WORKER_CONFIG[key])

        # Init config
        for key in DEFAULTS:
            setattr(self, key, self.config_get(key) or DEFAULTS[key])

        # Pools
        self.workers_pool = gevent.pool.Pool(self.workers_max)
        self.retry_workers_pool = gevent.pool.Pool(self.retry_workers_max)
        self.filter_workers_pool = gevent.pool.Pool(self.filter_workers_count)

        # Queues
        if self.input_queue_size == -1:
            self.input_queue = Queue()
        else:
            self.input_queue = Queue(self.input_queue_size)
        if self.resource_items_queue_size == -1:
            self.resource_items_queue = Queue()
        else:
            self.resource_items_queue = Queue(self.resource_items_queue_size)
        self.api_clients_queue = Queue()
        # self.retry_api_clients_queue = Queue()
        if self.retry_resource_items_queue_size == -1:
            self.retry_resource_items_queue = Queue()
        else:
            self.retry_resource_items_queue = Queue(
                self.retry_resource_items_queue_size)

        self.process = psutil.Process(os.getpid())

        if self.api_host != '' and self.api_host is not None:
            api_host = urlparse(self.api_host)
            if api_host.scheme == '' and api_host.netloc == '':
                raise DataBridgeConfigError(
                    'Invalid \'tenders_api_server\' url.')
        else:
            raise DataBridgeConfigError('In config dictionary empty or missing'
                                        ' \'tenders_api_server\'')
        self.db = prepare_couchdb(self.couch_url, self.db_name, logger)
        db_url = self.couch_url + '/' + self.db_name
        prepare_couchdb_views(db_url, self.workers_config['resource'], logger)
        collector_config = {
            'main': {
                'storage': 'couchdb',
                'couch_url': self.couch_url,
                'log_db': self.log_db_name
            }
        }
        self.server = Server(self.couch_url,
                             session=Session(retry_delays=range(10)))
        # self.logger = LogsCollector(collector_config)
        self.view_path = '_design/{}/_view/by_dateModified'.format(
            self.workers_config['resource'])
        extra_params = {
            'mode': self.retrieve_mode,
            'limit': self.resource_items_limit
        }
        self.feeder = ResourceFeeder(host=self.api_host,
                                     version=self.api_version, key='',
                                     resource=self.workers_config['resource'],
                                     extra_params=extra_params,
                                     retrievers_params=self.retrievers_params,
                                     adaptive=True)
        self.api_clients_info = {}
        # self.retry_api_clients_info = {}

    def config_get(self, name):
        try:
            return self.config.get('main').get(name)
        except AttributeError:
            raise DataBridgeConfigError('In config dictionary missed section'
                                        ' \'main\'')

    def create_api_client(self):
        client_user_agent = self.user_agent + '/' + self.bridge_id
        timeout = 0.1
        while 1:
            try:
                api_client = APIClient(
                    host_url=self.api_host, user_agent=client_user_agent,
                    api_version=self.api_version, key='',
                    resource=self.workers_config['resource'])
                client_id = uuid.uuid4().hex
                logger.info('Started api_client {}'.format(
                    api_client.session.headers['User-Agent']),
                    extra={'MESSAGE_ID': 'create_api_clients',
                           'type': 'counter'})
                api_client_dict = {
                    'id': client_id,
                    'client': api_client,
                    'request_interval': 0,
                    'not_actual_count': 0
                }
                self.api_clients_info[api_client_dict['id']] = {
                    'destroy': False,
                    'request_durations': {},
                    'request_interval': 0,
                    'avg_duration': 0
                }
                self.api_clients_queue.put(api_client_dict)
                break
            except RequestFailed as e:
                logger.error(
                    'Failed start api_client with status code {}'.format(
                        e.status_code),
                    extra={'MESSAGE_ID': 'exceptions', 'type': 'counter'})
                timeout = timeout * 2
                logger.info('create_api_client will be sleep {} sec.'.format(
                    timeout))
                sleep(timeout)
            except Exception as e:
                logger.error(
                    'Failed start api client with error: {}'.format(e.message),
                    extra={'MESSAGE_ID': 'exceptions', 'type': 'counter'})
                timeout = timeout * 2
                logger.info('create_api_client will be sleep {} sec.'.format(
                    timeout))
                sleep(timeout)

    def fill_api_clients_queue(self):
        while self.api_clients_queue.qsize() < self.workers_min:
            self.create_api_client()

    def fill_input_queue(self):
        for resource_item in self.feeder.get_resource_items():
            self.input_queue.put(resource_item)
            logger.debug('Add to temp queue from sync: {} {} {}'.format(
                self.workers_config['resource'][:-1], resource_item['id'],
                resource_item['dateModified']),
                extra={'MESSAGE_ID': 'received_from_sync', 'type': 'counter'})

    def send_bulk(self, input_dict):
        sleep_before_retry = 2
        for i in xrange(0, 3):
            try:
                rows = self.db.view(self.view_path, keys=input_dict.values())
                resp_dict = {k.id: k.key for k in rows}
                break
            except (IncompleteRead, Exception) as e:
                logger.error('Error while send bulk {}'.format(e.message),
                             extra={'MESSAGE_ID': 'exceptions',
                                    'type': 'counter'})
                if i == 2:
                    raise e
                sleep(sleep_before_retry)
                sleep_before_retry *= 2
        for item_id, date_modified in input_dict.items():
            if item_id in resp_dict and date_modified == resp_dict[item_id]:
                logger.debug('Ignored {} {}: SYNC - {}, EDGE - {}'.format(
                    self.workers_config['resource'][:-1], item_id,
                    date_modified, resp_dict[item_id]),
                    extra={'MESSAGE_ID': 'skiped', 'type': 'counter'})
            else:
                self.resource_items_queue.put({
                    'id': item_id,
                    'dateModified': date_modified
                })
                logger.debug('Put to main queue {}: {} {}'.format(
                    self.workers_config['resource'][:-1], item_id,
                    date_modified),
                    extra={'MESSAGE_ID': 'add_to_resource_items_queue',
                           'type': 'counter'})

    def fill_resource_items_queue(self):
        start_time = datetime.now()
        input_dict = {}
        while True:
            # Get resource_item from temp queue
            if not self.input_queue.empty():
                resource_item = self.input_queue.get()
            else:
                timeout = self.bulk_query_interval -\
                    (datetime.now() - start_time).total_seconds()
                if timeout > self.bulk_query_interval:
                    timeout = self.bulk_query_interval
                try:
                    resource_item = self.input_queue.get(timeout=timeout)
                except Empty:
                    resource_item = None

            # Add resource_item to bulk
            if resource_item is not None:
                input_dict[resource_item['id']] = resource_item['dateModified']

            if (len(input_dict) >= self.bulk_query_limit or
                (datetime.now() - start_time).total_seconds() >=
                    self.bulk_query_interval):
                if len(input_dict) > 0:
                    self.send_bulk(input_dict)
                    input_dict = {}
                start_time = datetime.now()

    def resource_items_filter(self, r_id, r_date_modified):
        try:
            local_document = self.db.get(r_id)
            if local_document:
                if local_document['dateModified'] < r_date_modified:
                    return True
                else:
                    return False
            else:
                return True
        except Exception as e:
            logger.error(
                'Filter error: Error while getting {} {} from couchdb: '
                '{}'.format(self.workers_config['resource'][:-1], r_id,
                            e.message),
                extra={'MESSAGE_ID': 'exceptions', 'type': 'counter'})
            return True

    def _get_average_requests_duration(self):
        req_durations = []
        delta = timedelta(seconds=self.perfomance_window)
        current_date = datetime.now() - delta
        for cid, info in self.api_clients_info.items():
            if len(info['request_durations']) > 0:
                if min(info['request_durations'].keys()) <= current_date:
                    info['grown'] = True
                avg = round(
                    sum(info['request_durations'].values()) * 1.0 / len(
                        info['request_durations']), 3)
                req_durations.append(avg)
                info['avg_duration'] = avg

        if len(req_durations) > 0:
            return round(sum(req_durations) / len(
                req_durations), 3), req_durations
        else:
            return 0, req_durations

    def bridge_stats(self):
        # sync_forward_last_response =\
        #     (datetime.now() - self.feeder.forward_info.get(
        #         'last_response', datetime.now())).total_seconds()
        # if self.feeder.backward_info.get('status') == FINISHED:
        #     sync_backward_last_response = 0
        # else:
        #     sync_backward_last_response =\
        #         (datetime.now() - self.feeder.backward_info.get(
        #             'last_response', datetime.now())).total_seconds()
        # stats_dict = {k: v for k, v in self.log_dict.items()}
        avg_request_duration, avg_list = self._get_average_requests_duration()
        avg_request_duration = avg_request_duration * 1000
        logger.debug(
            'Avg. requests duration {} milliseconds'.format(
                avg_request_duration),
            extra={'MESSAGE_ID': 'avg_request_duration',
                   'type': 'dimension',
                   'value': avg_request_duration})

        if len(avg_list) > 0:
            min_avg_request_duration = round(min(avg_list), 3) * 1000
            max_avg_request_duration = round(max(avg_list), 3) * 1000
        else:
            min_avg_request_duration = 0
            max_avg_request_duration = 0
        logger.debug(
            'Min. avg. request duration {} milliseconds'.format(
                min_avg_request_duration),
            extra={'MESSAGE_ID': 'min_avg_request_duration',
                   'type': 'dimension',
                   'value': min_avg_request_duration})
        logger.debug(
            'Max. avg. request duration {} milliseconds'.format(
                max_avg_request_duration),
            extra={'MESSAGE_ID': 'max_avg_request_duration',
                   'type': 'dimension',
                   'value': max_avg_request_duration})
        logger.info(
            'Resource items queue size {} items'.format(
                self.resource_items_queue.qsize()),
            extra={'MESSAGE_ID': 'resource_items_queue_size',
                   'type': 'dimension',
                   'value': self.resource_items_queue.qsize()})
        logger.info(
            'Retry resource items queue size {} items'.format(
                self.retry_resource_items_queue.qsize()),
            extra={'MESSAGE_ID': 'retry_resource_items_queue',
                   'type': 'dimension',
                   'value': self.retry_resource_items_queue.qsize()})
        workers_count = self.workers_max - self.workers_pool.free_count()
        logger.info('Main threads count {}'.format(workers_count),
                    extra={'MESSAGE_ID': 'workers_count',
                           'type': 'dimension',
                           'value': workers_count})
        if self.filler.exception:
            logger.info('Fill thread stoped with exception: {}'.format(
                self.filler.exception.message),
                extra={'MESSAGE_ID': 'filter_workers_count',
                       'type': 'dimension',
                       'value': 0})
        else:
            logger.info('Fill thread work normal',
                        extra={'MESSAGE_ID': 'filter_workers_count',
                               'type': 'dimension',
                               'value': 1})
        retry_workers_count = self.retry_workers_max -\
            self.retry_workers_pool.free_count()
        logger.info('Retry workers count'.format(retry_workers_count),
                    extra={'MESSAGE_ID': 'retry_workers_count',
                           'type': 'dimension',
                           'value': retry_workers_count})
        api_clients_count = len(self.api_clients_info)
        logger.info('Api clients count {}'.format(api_clients_count),
                    extra={'MESSAGE_ID': 'api_clients_count',
                           'type': 'dimension',
                           'value': api_clients_count})
        # stats_dict['api_clients_count'] = len(self.api_clients_info)
        # stats_dict['sync_queue'] = self.feeder.queue.qsize()
        # stats_dict['sync_forward_response_len'] =\
        #     self.feeder.forward_info.get('resource_item_count', 0)
        # stats_dict['sync_backward_response_len'] =\
        #     self.feeder.backward_info.get('resource_item_count', 0)
        # stats_dict['sync_forward_last_response'] = sync_forward_last_response
        # stats_dict['sync_backward_last_response'] =\
        # sync_backward_last_response
        # return stats_dict

    def queues_controller(self):
        while True:
            if (self.workers_pool.free_count() > 0 and
                (self.resource_items_queue.qsize() >
                 ((float(self.resource_items_queue_size) / 100) *
                  self.workers_inc_threshold))):
                self.create_api_client()
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.api_clients_info)
                self.workers_pool.add(w)
                logger.info('Queue controller: Create main queue worker.')
            elif (self.resource_items_queue.qsize() <
                  ((float(self.resource_items_queue_size) / 100) *
                   self.workers_dec_threshold)):
                if len(self.workers_pool) > self.workers_min:
                    wi = self.workers_pool.greenlets.pop()
                    wi.shutdown()
                    api_client_dict = self.api_clients_queue.get()
                    del self.api_clients_info[api_client_dict['id']]
                    logger.info('Queue controller: Kill main queue worker.')
            filled_resource_items_queue = round(
                self.resource_items_queue.qsize() /
                (float(self.resource_items_queue_size) / 100),
                2)
            logger.info('Resource items queue filled on {} %'.format(
                filled_resource_items_queue))
            filled_retry_resource_items_queue \
                = round(self.retry_resource_items_queue.qsize() / float(
                    self.retry_resource_items_queue_size) / 100, 2)
            logger.info('Retry resource items queue filled on {} %'.format(
                filled_retry_resource_items_queue))
            sleep(self.queues_controller_timeout)

    def gevent_watcher(self):
        self.perfomance_watcher()
        for t in self.server.tasks():
            if (t['type'] == 'indexer' and t['database'] == self.db_name and
                    t.get('design_document', None) == '_design/{}'.format(
                        self.workers_config['resource'])):
                logger.info(
                    'Watcher: Waiting for end of view indexing. Current'
                    ' progress: {} %'.format(t['progress']))
        self.bridge_stats()
        # spawn(self.logger.save, self.bridge_stats())
        # self.reset_log_counters()

        # Check fill threads
        if self.input_queue_filler.exception:
            logger.error('Temp queue filler error: {}'.format(
                self.input_queue_filler.exception.message),
                extra={'MESSAGE_ID': 'exceptions', 'type': 'counter'})
            self.input_queue_filler = spawn(self.fill_input_queue)
        if self.filler.exception:
            logger.error('Fill thread error: {}'.format(
                self.filler.exception.message),
                extra={'MESSAGE_ID': 'exceptions', 'type': 'counter'})
            self.filler = spawn(self.fill_resource_items_queue)

        if len(self.workers_pool) < self.workers_min:
            for i in xrange(0, (self.workers_min - len(self.workers_pool))):
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.api_clients_info)
                self.workers_pool.add(w)
                logger.info('Watcher: Create main queue worker.')
                self.create_api_client()
        if len(self.retry_workers_pool) < self.retry_workers_min:
            for i in xrange(0, self.retry_workers_min -
                            len(self.retry_workers_pool)):
                self.create_api_client()
                w = ResourceItemWorker.spawn(self.api_clients_queue,
                                             self.retry_resource_items_queue,
                                             self.db, self.workers_config,
                                             self.retry_resource_items_queue,
                                             self.api_clients_info)
                self.retry_workers_pool.add(w)
                logger.info('Watcher: Create retry queue worker.')

    def _calculate_st_dev(self, values):
        if len(values) > 0:
            avg = sum(values) * 1.0 / len(values)
            variance = map(lambda x: (x - avg) ** 2, values)
            avg_variance = sum(variance) * 1.0 / len(variance)
            st_dev = math.sqrt(avg_variance)
            return round(st_dev, 3)
        else:
            return 0

    def _mark_bad_clients(self, dev):
        # Mark bad api clients
        for cid, info in self.api_clients_info.items():
            if info.get('grown', False) and info['avg_duration'] > dev:
                info['destroy'] = True
                self.create_api_client()
                logger.debug(
                    'Perfomance watcher: Mark client {} as bad, avg.'
                    ' request_duration is {} sec.'.format(
                        cid, info['avg_duration']),
                    extra={'MESSAGE_ID': 'lazy_clients', 'type': 'counter'})
            elif info['avg_duration'] < dev and info['request_interval'] > 0:
                self.create_api_client()
                info['destroy'] = True
                logger.debug(
                    'Perfomance watcher: Mark client {} as bad,'
                    ' request_interval is {} sec.'.format(
                        cid, info['request_interval']),
                    extra={'MESSAGE_ID': 'lazy_clients', 'type': 'counter'})

    def perfomance_watcher(self):
            avg_duration, values = self._get_average_requests_duration()
            for _, info in self.api_clients_info.items():
                delta = timedelta(
                    seconds=self.perfomance_window + self.watch_interval)
                current_date = datetime.now() - delta
                delete_list = []
                for key in info['request_durations']:
                    if key < current_date:
                        delete_list.append(key)
                for k in delete_list:
                    del info['request_durations'][k]
                delete_list = []

            st_dev = self._calculate_st_dev(values)
            dev = round(st_dev + avg_duration, 3)
            logger.info(
                'Perfomance watcher: Standart deviation for '
                'request_duration is {} sec.'.format(round(st_dev, 3)),
                extra={'MESSAGE_ID': 'request_dev',
                       'type': 'dimension',
                       'value': dev * 1000})
            self._mark_bad_clients(dev)
            clear_api_client_queue(self.api_clients_queue,
                                   self.api_clients_info)

    def run(self):
        logger.info('Start Edge Bridge',
                    extra={'MESSAGE_ID': 'edge_bridge_start_bridge'})
        logger.info('Start data sync...',
                    extra={'MESSAGE_ID': 'edge_bridge__data_sync'})
        self.input_queue_filler = spawn(self.fill_input_queue)
        self.filler = spawn(self.fill_resource_items_queue)
        spawn(self.queues_controller)
        while True:
            self.gevent_watcher()
            sleep(self.watch_interval)


def main():
    parser = argparse.ArgumentParser(description='---- Edge Bridge ----')
    parser.add_argument('config', type=str, help='Path to configuration file')
    params = parser.parse_args()
    if os.path.isfile(params.config):
        with open(params.config) as config_file_obj:
            config = load(config_file_obj.read())
        logging.config.dictConfig(config)
        EdgeDataBridge(config).run()


##############################################################

if __name__ == "__main__":
    main()
