import asyncio
import aioredis
from bs4 import BeautifulSoup
import re
from .settings import config
from .helpers import normalize_url
from .cli import args as cli_args
from .balancer import LoadBalancer
from .requests import Request
from .exceptions import BadResponse
from .utils import State
import signal
import sys


regex = re.compile(
    r'(^(\/\w+)+)|' #relative url support
    r'^(?:http|ftp)s?://'  # http:// or https://
    # domain...
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
    r'localhost|'  # localhost...
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}|'  # ...or ipv4
    r'\[?[A-F0-9]*:[A-F0-9:]+\]?)'  # ...or ipv6
    r'(?::\d+)?'  # optional port
    r'(?:/?|[/?]\S+)$', re.IGNORECASE)


class URLDispatcher:
    '''
    Simple URL dispatcher storing URLs in-memory
    '''

    def __init__(self):
        self.to_visit = set()
        self.visited = set()

    @asyncio.coroutine
    def init(self):
        pass

    @asyncio.coroutine
    def add_to_visit(self, url):
        if url not in self.visited:
            self.to_visit.update([url])

    @asyncio.coroutine
    def get_url(self):
        while len(self.to_visit) == 0:
            yield from asyncio.sleep(0.1)
        url = self.to_visit.pop()
        yield from self.add_to_visited(url)
        return url

    @asyncio.coroutine
    def add_to_visited(self, url):
        self.visited.update([url])


class RedisURLDispatcher:
    '''
    Class responsible for feeding crawlers with URLs (stored in redis)
    to visit
    '''

    def __init__(self, cm, loop=None):
        self.loop = loop or asyncio.get_event_loop()
        self.cm = cm
        self.url = config['redis']['url']
        self.port = int(config['redis']['port'])
        self.connection = None
        self.to_visit, self.visited = config['redis']['to_visit'], \
            config['redis']['visited']

    @asyncio.coroutine
    def init(self):
        self.connection = yield from aioredis.create_connection((self.url, self.port), loop=self.loop, encoding='utf-8')

    @asyncio.coroutine
    def add_to_visit(self, url):
        in_visited = yield from self.connection.execute('sismember', self.visited, url)
        if not in_visited == 1:
            yield from self.connection.execute('sadd', self.to_visit, url)

    @asyncio.coroutine
    def get_url(self):
        url = yield from self.connection.execute('spop', self.to_visit)
        while url is None:
            left = yield from self.urls_left()
            # if there's no url left and ALL crawlers are (or will be) trying to get one - start stopping procedure
            # there's not chance that new url will appear
            if all([crawler.state <= State('getting_url') for crawler in self.cm.crawlers]) and not left:
                if not self.cm.state == 'stopped':
                    print('stoppowanko')
                    self.cm._quit_handler(None, None)
                return None
            yield from asyncio.sleep(0.3)
            url = yield from self.connection.execute('spop', self.to_visit)
        yield from self.add_to_visited(url)
        return url

    @asyncio.coroutine
    def add_to_visited(self, url):
        yield from self.connection.execute('sadd', self.visited, url)

    @asyncio.coroutine
    def urls_left(self):
        return (yield from self.connection.execute('scard', self.to_visit))


class Crawler:
    state = State('created')

    def __init__(self, cm, urldis, data_processor, load_balancer):
        self.future = asyncio.Future()
        self.cm = cm
        self.urldis = urldis
        self.url_constraints = []
        self.data_processor = data_processor
        self.load_balancer = load_balancer

    def set_url_constraint(self, constraint):
        self.url_constraints = [constraint]

    def append_url_constraint(self, constraint):
        self.url_constraints.append(constraint)

    def correct_urls_iterator(self, html, visited):
        for g_url in self.get_urls(html):
            try:
                n_url = normalize_url(g_url, visited=visited)
            except Exception as e:
                print(e)
                continue
            bad_url = False
            for constraint in self.url_constraints:
                if not constraint(n_url):
                    bad_url = True
            if bad_url:
                continue
            yield n_url

    @asyncio.coroutine
    def crawl(self):
        yield from self.cm.acquire()
        yield from self.load_balancer.ask()
        self.state = State('getting_url')
        url = yield from self.urldis.get_url()
        if not url:
            self._done()
            return
        site_downloader = Request(url).download()
        self.state = State('downloading_site')
        try:
            html = yield from site_downloader
        except BadResponse:
            print('Crawler got bad response')
            return
        except Exception as e:
            print('{} - could not be scrapped'.format(url))
            import pdb; pdb.set_trace()
            print(e)
            return
        print(url)
        self.state = State('pushing_urls')
        for url in self.correct_urls_iterator(html, url):
            yield from self.urldis.add_to_visit(url)
        self.state = State('feeding_data')
        yield from self.data_processor.feed_with_data(html)
        self._done()

    def _done(self):
        self.cm.release()
        self.future.set_result(None)
        self.state = State('done')

    def get_urls(self, html):
        soup = BeautifulSoup(html, 'lxml')
        ass = soup.find_all('a')
        for i in range(len(ass)):
            try:
                href = ass[i].get('href')
                if regex.match(href):
                    yield href
            except:
                pass


class CrawlersManager:
    CONCURRENT_MAX = 10
    url_constraints = []
    state = 'running'
    crawlers = []

    def __init__(self, data_processor):
        self.future = asyncio.Future()
        self.start_url = config['scraper']['start_url']
        self.loop = asyncio.get_event_loop()
        self.url_dispatcher = RedisURLDispatcher(self)
        self.semaphore = asyncio.Semaphore(value=self.CONCURRENT_MAX)
        self.concurrent = 0
        self.data_processor = data_processor
        self.load_balancer = LoadBalancer()
        signal.signal(signal.SIGINT, self._quit_handler)
        self._parse_cli()
        self._do_some_init()

    def _quit_handler(self, signal, frame):
        print('\nAll crawlers are being stopped')
        output = sys.stdout
        sys.stdout = None #silence crawlers

        @asyncio.coroutine
        def closing_task():
            yield from self.stop()
            sys.stdout = output
            print('Crawlers and connections closed')

        asyncio.ensure_future(closing_task())

    @property
    def connection(self):
        return self.data_processor.storage.connection

    def _clear_db(self):
        self.loop.run_until_complete(self.connection.execute('flushdb'))

    def _do_some_init(self):
        self.loop.run_until_complete(self.url_dispatcher.init())
        if self.start_url:
            self.loop.run_until_complete(self.url_dispatcher.add_to_visit(
                self.start_url))

    def _parse_cli(self):
        if cli_args.concurrent:
            self.CONCURRENT_MAX = cli_args.concurrent
        if cli_args.slave:
            self.start_url = None
        if cli_args.clear:
            self._clear_db()

    def _close_connections(self):
        self.url_dispatcher.connection.close()
        self.data_processor.storage.connection.close()

    def set_url_constraint(self, constraint):
        self.url_constraints = [constraint]

    def append_url_constraint(self, constraint):
        self.url_constraints.append(constraint)

    def set_concurrent_crawlers(self, n):
        self.CONCURRENT_MAX = n

    def _new_crawler(self):
        c = Crawler(self, self.url_dispatcher, self.data_processor, self.load_balancer)
        c.url_constraints = self.url_constraints
        return c

    @asyncio.coroutine
    def acquire(self):
        yield from self.semaphore.acquire()
        self.concurrent += 1

    def release(self):
        self.semaphore.release()
        self.concurrent -= 1

    def fire_one(self):

        def done_callback(crawler_task):
            asyncio.Task(self.crawler_done(crawler_task))

        crawler = self._new_crawler()
        crawler.future.add_done_callback(lambda res: done_callback(crawler))
        asyncio.ensure_future(crawler.crawl())
        return crawler

    @asyncio.coroutine
    def crawler_done(self, crawler_task):
        self.crawlers.remove(crawler_task)
        if self.state == 'running':
            self.crawlers.append(self.fire_one())

    @asyncio.coroutine
    def run(self):
        self.state = 'running'
        for _ in range(self.CONCURRENT_MAX):
            self.crawlers.append(self.fire_one())
        yield from self.future

    @asyncio.coroutine
    def stop(self):
        self.state = 'stopped'
        self.set_concurrent_crawlers(0)
        yield from asyncio.wait(map(lambda c: c.future, self.crawlers))
        self._close_connections()
        self.crawlers = []
        self.future.set_result(True)
