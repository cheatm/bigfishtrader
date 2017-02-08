import tushare
import json
import pandas as pd
import oandapy
from datetime import datetime
from threading import Thread
try:
    from Queue import Queue, Empty
except:
    from queue import Queue, Empty


class DataCollector(object):
    def __init__(self, **setting):
        from pymongo import MongoClient

        db = setting.pop('db')
        users = setting.pop('user', {})
        self.client = MongoClient(**setting)
        self.db = self.client[db]

        for db in users:
            self.client[db].authenticate(users[db]['id'], users[db]['password'])

        self._running = False
        self.queue = Queue()
        self._threads = {}

    def save(self, col_name, data):
        data = [doc.to_dict() for index, doc in data.iterrows()] if isinstance(data, pd.DataFrame) else data
        deleted = 0
        for doc in data:
            db_doc = self.db[col_name].find_one({'datetime': doc['datetime']})
            if db_doc:
                self.db[col_name].delete_one({'datetime': doc['datetime']})
                deleted += 1
            else:
                break
        for doc in reversed(data):
            db_doc = self.db[col_name].find_one({'datetime': doc['datetime']})
            if db_doc:
                self.db[col_name].delete_one({'datetime': doc['datetime']})
                deleted += 1
            else:
                break

        self.db[col_name].insert(data)
        self.db[col_name].create_index('datetime')
        return [col_name, data[0]['datetime'], data[-1]['datetime'], len(data), deleted]

    def run(self, function):
        while self._running or self.queue.qsize():
            try:
                params = self.queue.get(timeout=1)
            except Empty:
                continue
            result = function(**params)
            if result is not None:
                print result

    def start(self, function, t=5):
        self._running = True
        for i in range(0, t):
            thread = Thread(target=self.run, args=[function])
            thread.start()
            self._threads[thread.name] = thread

    def join(self):
        for name, thread in self._threads.items():
            thread.join()

        while len(self._threads):
            self._threads.popitem()

    def stop(self):
        self._running = False


class TushareData(DataCollector):
    def __init__(self, **setting):
        setting.setdefault('db', 'HS')
        super(TushareData, self).__init__(**setting)

    def save_k_data(
            self, code=None, start='', end='',
            ktype='D', autype='qfq', index=False,
            retry_count=3, pause=0.001
    ):
        frame = tushare.get_k_data(
            code, start, end,
            ktype, autype, index,
            retry_count, pause
        )

        format_ = '%Y-%m-%d'
        if len(frame['date'].values[-1]) > 11:
            format_ = ' '.join((format_, '%H:%M'))

        frame['datetime'] = pd.to_datetime(
            frame.pop('date'),
            format=format_
        )

        frame.pop('code')

        self.save('.'.join((code, ktype)), frame)

    def update(self, col_name):
        doc = self.db[col_name].find_one(sort=[('datetime', -1)])
        code, ktype = col_name.split('.')
        try:
            self.save_k_data(code, start=doc['datetime'].strftime('%Y-%m-%d %H:%M'), ktype=ktype)
        except IndexError:
            print (col_name, 'already updated')

    def update_all(self):
        for collection in self.db.collection_names():
            self.update(collection)

    def save_hs300(
            self, start='', end='',
            ktype='D', autype='qfq', index=False,
            retry_count=3, pause=0.001
        ):
        hs300 = tushare.get_hs300s()
        for code in hs300['code']:
            self.save_k_data(
                code, start, end,
                ktype, autype, index,
                retry_count, pause
            )


class OandaData(DataCollector):
    def __init__(self, oanda_info, **setting):
        """

        :param oanda_info: dict, oanda account info {'environment': 'practice', 'access_token': your access_token}
        :param setting:
        :return:
        """

        setting.setdefault('db', 'Oanda')
        super(OandaData, self).__init__(**setting)

        if isinstance(oanda_info, str):
            with open(oanda_info) as info:
                oanda_info = json.load(info)
                info.close()

        self.api = oandapy.API(oanda_info['environment'], oanda_info['access_token'])
        self.time_format = '%Y-%m-%dT%H:%M:%S.%fZ'

    def get_history(self, instrument, **kwargs):
        data_type = kwargs.pop('data_type', 'dict')
        if isinstance(kwargs.get('start', None), datetime):
            kwargs['start'] = kwargs['start'].strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        if isinstance(kwargs.get('end', None), datetime):
            kwargs['end'] = kwargs['end'].strftime('%Y-%m-%dT%H:%M:%S.%fZ')

        kwargs.setdefault('candleFormat', 'midpoint')
        result = self.api.get_history(instrument=instrument, **kwargs)

        for candle in result['candles']:
            candle['datetime'] = datetime.strptime(candle['time'], '%Y-%m-%dT%H:%M:%S.%fZ')

        if data_type == 'DataFrame':
            result['candles'] = pd.DataFrame(result['candles'])

        return result

    def save_history(self, instrument, **kwargs):
        try:
            result = self.get_history(instrument, **kwargs)
        except oandapy.OandaError as oe:
            if oe.error_response['code'] == 36:
                return self.save_div(instrument, **kwargs)
            else:
                raise oe

        return self.save(
            '.'.join((result['instrument'], result['granularity'])),
            result['candles']
        )

    def save_div(self, instrument, **kwargs):
        if 'start' in kwargs:
            end = kwargs.pop('end', None)
            kwargs['count'] = 5000
            saved = self.save_history(instrument, **kwargs)
            print(saved)

            kwargs.pop('count')
            if end:
                kwargs['end'] = end
            kwargs['start'] = saved[2]
            next_saved = self.save_history(instrument, **kwargs)
            print(next_saved)
            saved[3] += next_saved[3]
            saved[4] += next_saved[4]
            saved[2] = next_saved[2]
            return saved
        else:
            raise ValueError('In save data mode, start is required')

    def save_manny(self, instruments, granularity, start, end=None, t=5):
        if isinstance(instruments, list):
            for i in instruments:
                self.queue.put({
                    'instrument': i,
                    'granularity': granularity,
                    'start': start,
                    'end': end
                })
        else:
            return self.save_history(instruments, granularity=granularity, start=start, end=end)

        self.start(self.save_history, t)
        self.stop()
        self.join()

    def update(self, col_name):
        doc = self.db[col_name].find_one(sort=[('datetime', -1)], projection=['time'])
        if doc is None:
            raise ValueError('Unable to find the last record or collection: %s, '
                             'please check your DataBase' % col_name)

        i, g = col_name.split('.')
        return self.save_history(i, granularity=g, start=doc['time'])

    def update_manny(self, *col_names, **others):
        if len(col_names) == 0:
            col_names = self.db.collection_names()

        for col_name in col_names:
            self.queue.put({'col_name': col_name})

        self.start(self.update, others.pop('t', 5))
        self.stop()
        self.join()


if __name__ == '__main__':
    oanda = OandaData("D:/bigfishtrader/bigfish_oanda.json", port=10001, db='Oanda_test')

    oanda.save_manny(['EUR_USD', 'USD_JPY', 'AUD_USD'], 'M30', datetime(2016, 1, 1), datetime.now())
    oanda.save_history('EUR_USD', granularity='H1', start=datetime(2016, 1, 1), end=datetime.now())
