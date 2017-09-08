#
# coding: utf-8
# Copyright (C) DATADVANCE, 2010-2018
#
"""
Module with tests for `Launcher` class.
"""

import asyncio
import base64
import collections
import inspect
import os
import random
import sys
import textwrap
import time
import uuid

import psutil
import pytest
import aiohttp

from .launcher import Launcher

# Disable warning that outer name is redefined: `pytest` dependency injection
# works this way.
# pylint: disable=redefined-outer-name


def test_main_usecase(eventloop, http_server):
    """Test main use-case.

    Test checks the following:
      - Two simple HTTP servers spawned by `Launcher`.
      - Both servers use random port number specified as callable in the
        `params` setting.
      - Substitutions like `{ServiceName_PARAM}` works in `cmd` and `env`
        settings.
      - Both servers correctly respond on the ports reported by `Launcher`.
      - Second process is given with dynamically determined parameter `port`
        of the first one.
      - Second server listens to two endpoints.
      - `Launcher` terminates all services when at least one of them stops.
    """

    # Configure two test servers.
    server1 = {
        'name': 'Server1',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
            'UNIQ': lambda: str(uuid.uuid1()),
        },
        'endpoints': [('{Server1_HOST}', '{Server1_PORT}')],
        'env': {
            'RESPONSE': '{Server1_UNIQ}'
        },
        'cmd': [
            sys.executable, '-u', http_server.script,
            '--host={Server1_HOST}', '--port={Server1_PORT}'
        ],
    }

    server2 = {
        'name': 'Server2',
        'params': {
            'PORT1': lambda: random.randint(49152, 65535),
            'PORT2': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
        },
        'endpoints': [('{Server2_HOST}', '{Server2_PORT1}'),
                      ('{Server2_HOST}', '{Server2_PORT2}')],
        'env': {
            'RESPONSE': '{Server1_HOST}:{Server1_PORT}'
        },
        'cmd': [
            sys.executable, '-u', http_server.script,
            '--host', '{Server2_HOST}',
            '--port', '{Server2_PORT1}', '{Server2_PORT2}'
        ],
    }

    # Create and start launcher.
    launcher = Launcher([server1, server2])
    try:
        eventloop(launcher.run())

        # Extract actual values of host and port from information provided by
        # launcher.
        server1_host = launcher.services['Server1'].params['HOST']
        server1_port = launcher.services['Server1'].params['PORT']
        server1_uniq = launcher.services['Server1'].params['UNIQ']
        server2_host = launcher.services['Server2'].params['HOST']
        server2_port1 = launcher.services['Server2'].params['PORT1']
        server2_port2 = launcher.services['Server2'].params['PORT2']

        # Make requests to both servers.
        server1_resp = eventloop(http_server.ping(server1_host, server1_port))
        server2_resp1 = eventloop(
            http_server.ping(server2_host, server2_port1))
        server2_resp2 = eventloop(
            http_server.ping(server2_host, server2_port2))

        # Stop one of two servers to check that second one will be terminated by
        # launcher.
        eventloop(http_server.stop(server1_host, server1_port))

        # Wait launcher to finish, it shall finish instantly because we have
        # stopped servers already.
        eventloop(launcher.wait())
    finally:
        # Shutdown launcher.
        eventloop(launcher.shutdown())

    # Check that first server responded with our unique response.
    assert server1_resp == server1_uniq

    # Check that the second server responded with correct information about the
    # first one, hence we check that context is shared properly between
    # services.
    assert (server2_resp1 == '{}:{}'.format(server1_host, server1_port) and
            server2_resp2 == '{}:{}'.format(server1_host, server1_port))


def test_service_listens_endpoint(eventloop, http_server):
    """Test the case when process did not listen to proper endpoint, but it is
       listened by someone.

    This tests configures two servers and make `Launcher` wait second server to
    serve endpoint which it does not serve. What is more important is this
    endpoint is served by the first server. We expect `Launcher` to interrupt
    starting procedure and exit. Hence we make sure that `Launcher` checks not
    only endpoint is served, but that it is served by the process it just run.
    """

    # Configure two test servers.
    server1_cfg = {
        'name':
        'Server1',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
        },
        'endpoints': [('{Server1_HOST}', '{Server1_PORT}')],
        'cmd': [
            sys.executable, '-u', http_server.script,
            '--host={Server1_HOST}', '--port={Server1_PORT}'
        ],
    }

    server2_cfg = {
        'name':
        'Server2',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
        },
        'endpoints': [('{Server1_HOST}', '{Server1_PORT}')],
        'endpoints_timeout':
        0.1,
        'cmd': [
            sys.executable, '-u', http_server.script,
            '--host={Server2_HOST}', '--port={Server2_PORT}'
        ],
    }

    launcher = Launcher([server1_cfg, server2_cfg])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())


def test_service_stopped_during_start(eventloop, print_sleep_script):
    """Test the case when service stopped during start procedure (within given
       time period).

    In such cases `Launcher` must interrupt service starting procedure and exit.
    """

    WAIT_DELAY = 0.5
    test_service = {
        'name': 'TestService',
        'params': {
            'DELAY': 0
        },
        'wait': WAIT_DELAY,
        'cmd': [sys.executable, '-u', print_sleep_script,
                '--delay={TestService_DELAY}'],
    }

    # Create launcher and check that it raises from the method `run`.
    launcher = Launcher([test_service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
    finally:
        # Shutdown launcher.
        eventloop(launcher.shutdown())


def test_service_stopped_very_fast(eventloop):
    """Test assures that if service dies immediately then it raises
       `RuntimeException`.

    This test is added to cover the case when process dies immediately (and very
    fast) after it starts. There was error when creating `psutil.Process` was
    not enclosed with try-except block (inside routing that process listens to
    the endpoint). This lead `Launcher.run()` to fail with
    `psutil.NoSuchProcess` exception and a lot of other errors in the log.

    To reproduce the error (until it is fixed) is important that process exits
    very fast, otherwise error is masked.
    """
    test_service = {
        'name': 'TestService',
        'endpoints': [('127.0.0.1', '4242')],
        'endpoints_timeout': 0.1,
        'cmd': ['true'],
    }

    # Create launcher and check that it raises from the method `run`.
    launcher = Launcher([test_service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
    finally:
        # Shutdown launcher.
        eventloop(launcher.shutdown())


def test_service_stopped_instead_of_listening(eventloop):
    """Test the case when process stops instead of starting and listening to
       the endpoint.

    Check that in such cases `Launcher` raises `RuntimeError` exception and
    stops.
    """

    test_service = {
        'name': 'TestService',
        'endpoints': [('127.0.0.1', '4242')],
        'cmd': [sys.executable, '-c', 'pass'],
    }

    # Create launcher and check that it raises from the method `run`.
    launcher = Launcher([test_service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
    finally:
        # Shutdown launcher.
        eventloop(launcher.shutdown())


def test_log_redirected(eventloop, print_sleep_script, caplog):
    """Test that service output is redirected to the `logging`.

    Check that `Launcher` redirects output to `logging` with level
    `logging.INFO`: capture all output and find line containing at the same time
    unique string, process id and the service name.

    Also check running `Launcher` with minimum number of settings.
    """

    unique_str = str(uuid.uuid1())

    test_service = {
        'name': 'TestService',
        'cmd': [sys.executable,
                '-u', print_sleep_script,
                '--print', unique_str],
    }

    launcher = Launcher([test_service])
    try:
        eventloop(launcher.run())
        pid = launcher.services['TestService'].process.pid
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())

    for r in caplog.record_tuples:
        if str(pid) in r[0] and 'TestService' in r[0] and unique_str in r[2]:
            break
    else:
        assert False, 'launcher logging output does not contain service output'


def test_service_child_process_listens_endpoint(eventloop, http_server):
    """Test that `Launcher` handles the case when service child process listens
       the endpoint."""

    test_service = {
        'name':
        'TestService',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'UNIQ': str(uuid.uuid1()),
        },
        'endpoints': [('127.0.0.1', '{TestService_PORT}')],
        'env': {
            'RESPONSE': '{TestService_UNIQ}'
        },
        'cmd': [
            sys.executable, '-c',
            '''import os;'''
            '''os.system(r'"{}" -u {} --host=127.0.0.1 --port={}')'''.format(
                sys.executable, http_server.script, '{TestService_PORT}'
            )
        ]
    }

    launcher = Launcher([test_service])
    try:
        eventloop(launcher.run())
        response = eventloop(
            http_server.ping(
                '127.0.0.1', launcher.services['TestService'].params['PORT'])
        )
        eventloop(http_server.stop(
            '127.0.0.1', launcher.services['TestService'].params['PORT']))
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())
    assert response == launcher.services['TestService'].params['UNIQ']


def test_endpoints_timeout(eventloop, print_sleep_script):
    """Test that `Launcher` respects `endpoints_timeout` setting.

    Check that `Launcher` respects `endpoints_timeout` setting: check that the
    dummy process works at least the amount of time specified in this setting.
    """
    TIMEOUT_1 = 1.
    TIMEOUT_2 = 2.

    test_service_1 = {
        'name': 'TestService1',
        'endpoints': [('127.0.0.1', '4242')],
        'endpoints_timeout': TIMEOUT_1,
        'cmd': [sys.executable, '-u', print_sleep_script, '--delay=60']
    }

    test_service_2 = {
        'name': 'TestService2',
        'endpoints': [('127.0.0.1', '4242')],
        'endpoints_timeout': TIMEOUT_2,
        'cmd': [sys.executable, '-u', print_sleep_script, '--delay=60']
    }

    # Sequentially start test service two times with different
    # `endpoints_timeout` settings. Measure actual time until `Launcher` raises
    # `RuntimeError` exception.
    launcher = Launcher([test_service_1])
    start_ts = time.time()
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())
    elapsed_1 = time.time() - start_ts

    launcher = Launcher([test_service_2])
    start_ts = time.time()
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())
    elapsed_2 = time.time() - start_ts

    # Check that the difference between these time periods is significant -
    # greater than at least half of timeouts difference.
    assert elapsed_2 - elapsed_1 > (TIMEOUT_2 - TIMEOUT_1) / 2


def test_process_exit(eventloop, http_server, print_sleep_script):
    """Test shutdown procedure when one of services terminates."""

    # Configure several HTTP servers and add service, which terminates in few
    # seconds.
    service_configs = []
    for i in range(7):
        server_cfg = {
            'name':
            'Server%s' % i,
            'params': {
                'PORT': lambda: random.randint(49152, 65535)
            },
            'endpoints': [('127.0.0.1', '{Server%s_PORT}' % i)],
            'cmd': [
                sys.executable, '-u', http_server.script,
                '--host=127.0.0.1', '--port={Server%s_PORT}' % i
            ],
        }
        service_configs += [server_cfg]

    service_configs += [{
        'name': 'BlackSheep',
        'cmd': [sys.executable, '-u', print_sleep_script, '--delay=1']
    }]

    launcher = Launcher(service_configs)
    try:
        eventloop(launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())


def test_environment(eventloop, print_env_script, caplog):
    """Test that `Launcher` correctly handle system environment.

    Test checks the following:
      - System environment passed to the started process.
      - Values from `env` setting is available in the process for both new and
        modified variables.
      - Setting some of `env` values to `None` removes it from environment.
    """

    # Setup three environment variables with globally unique names and values:
    # the first shall pass as is; value of the second will be modified; the
    # third will be removed.

    def uniq_gen():
        """Generate GUID encoded to base32 with trailing `=` removed."""
        return base64.b32encode(uuid.uuid4().bytes).replace(b'=', b'').decode()

    var_name_prefix = uniq_gen().upper()
    var_pass_name = var_name_prefix + uniq_gen().upper()
    var_pass_value = var_name_prefix + uniq_gen().lower()
    var_modify_name = var_name_prefix + uniq_gen().upper()
    var_modify_value = var_name_prefix + uniq_gen().lower()
    var_remove_name = var_name_prefix + uniq_gen().upper()
    var_remove_value = var_name_prefix + uniq_gen().lower()
    env = {
        var_pass_name: var_pass_value,
        var_modify_name: var_modify_value,
        var_remove_name: var_remove_value,
    }
    os.environ.update(env)

    env_printer = {
        'name': 'EnvPrinter',
        'env': {
            # preserve original name
            var_pass_name: var_pass_value,
            # modify name
            var_modify_name: uniq_gen().lower(),
            # remove variable
            var_remove_name: None,
        },
        'cmd': [sys.executable, '-u', print_env_script, var_name_prefix],
    }

    # Run launcher and capture all logs.
    launcher = Launcher([env_printer])
    try:
        eventloop(launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())

    # Check that name and value of first variable exists in output. Since we
    # rely on global uniqueness of variable names and values we can simply check
    # their existence as substring.
    assert var_pass_name in caplog.text, (
        'expected environment variable not found')
    assert var_pass_value in caplog.text, (
        'value of expected environment variable not found')
    assert var_modify_name in caplog.text, (
        'environment variable must be modified, but absent')
    assert var_modify_value not in caplog.text, (
        'value that must be changed found in environment')
    assert var_remove_name not in caplog.text, (
        'removed variable found in environment')
    assert var_remove_value not in caplog.text, (
        'value of removed variable found in environment')


def test_attempts(eventloop, print_env_script, caplog):
    """Make sure `Launcher` respects the value of `attempts` setting.

    Generate globally unique value and put it into environment. Run test script
    which prints environment and make `Launcher` wait until it starts serving
    endpoint. Since script will not do this, `Launcher` must try to restart the
    script several times. To check the number of times `Launcher` does this we
    count number of occurrences of the unique values in the output.
    """

    # Generate globally unique value.
    uniq = base64.b32encode(uuid.uuid4().bytes).replace(
        b'=', b'').decode().lower()

    experiments = [1, 2, 4]
    for attempts in experiments:
        env_printer = {
            'name': 'EnvPrinter',
            'env': {
                'VARIABLE': uniq
            },
            'wait': 5,
            'attempts': attempts,
            'cmd': [sys.executable, '-u', print_env_script, uniq],
        }

        # Run launcher.
        launcher = Launcher([env_printer])
        try:
            pytest.raises(RuntimeError, eventloop, launcher.run())
            eventloop(launcher.wait())
        finally:
            eventloop(launcher.shutdown())

    # Check that unique value appears exactly proper number of times in the test
    # output.
    assert str(caplog.text).count(uniq) == sum(
        experiments), 'wrong number of attempts to run service'


def test_launcher_name(eventloop, caplog):
    """Check that `Launcher` uses `name` argument as logger name"""

    unique_str = str(uuid.uuid1())

    test_service = {
        'name': 'TestService',
        'cmd': [sys.executable, '-c', 'pass'],
    }

    launcher = Launcher([test_service], name=unique_str)
    try:
        eventloop(launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())

    for record in caplog.records:
        if record.name == unique_str:
            break
    else:
        assert False, 'Launcher name is not found in the log output'


def test_config_validation():
    """Test proper handling of improper configuration parameters."""

    def check_fails(configs):
        """Utility function to check that given configuration leads to
           `AssertionError`"""
        pytest.raises((AssertionError, ValueError, TypeError, IndexError),
                      Launcher, configs)

    noop_cmd = [sys.executable, '-c', 'pass']

    # Check empty config.
    check_fails([{}])

    # Check incomplete config: no `name`.
    check_fails([{'name': 'A'}])

    # Check incomplete config: no `cmd`.
    check_fails([{'cmd': ['a', 'b']}])

    # Check extra key
    check_fails([{'name': 'NoOp', 'cmd': [sys.executable, '-c', 'pass'],
                  'somestr': 42}])

    # Check empty command line
    check_fails([{'name': 'NoOp', 'cmd': []}])

    # Check wrong value: cmd
    check_fails([{'name': 'NoOp', 'cmd': 'somestr'}])

    # Check wrong value: attempts
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'attempts': -1}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'attempts': 'somestr'}])

    # Check wrong value: endpoints_timeout
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'endpoints_timeout': -1}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd,
                  'endpoints_timeout': 'somestr'}])

    # Check wrong value: wait
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'wait': -1}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'wait': 'somestr'}])

    # Check wrong value: endpoint
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'endpoints': 1}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'endpoints': 'somestr'}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'endpoints': ('', 42)}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd,
                  'endpoints': [('1.3.4.5', '')]}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd,
                  'endpoints': ('1.3.4.5', '')}])


# ------------------------------------------------------------- UTILITY FIXTURES


@pytest.fixture(scope='function')
def eventloop():
    """Fixture provides eventloop to tests and perform cleanup checks.

    Fixture provides eventloop to tests and check that test did not leave child
    processes or pending tasks. It yields callable which accepts coroutine or
    future and wait until it completes. It is needed to `await` in synchronous
    code.

    Yields:
        Callable which wait given coroutine or future to complete from
        synchronous code.
    """

    # Setup new even loop for each test.
    if sys.platform == 'win32':
        # in Windows setup even loop which support pipes:
        # https://docs.python.org/3/library/asyncio-subprocess.html
        loop = asyncio.ProactorEventLoop()
        asyncio.set_event_loop(loop)
    else:
        loop = asyncio.SelectorEventLoop()
        asyncio.set_event_loop(loop)

    # Run test providing it the way to await for futures or coroutines.
    yield asyncio.get_event_loop().run_until_complete

    # Check there is no child processes left behind when test function finishes,
    # cleanup event loop.

    children = psutil.Process().children(recursive=True)
    assert not children, 'test has left child processes'

    loop = asyncio.get_event_loop()
    loop.run_until_complete(loop.shutdown_asyncgens())
    tasks = asyncio.Task.all_tasks(loop)
    pending_tasks = [task for task in tasks if not task.done()]
    assert not pending_tasks, 'test has left pending tasks in the event loop'
    loop.close()


@pytest.fixture(scope='session')
def http_server(tmpdir_factory):
    """Prepare test HTTP server: Python file with code, and utility functions
       for request and stop.

    This method returns a path to Python script with simple HTTP server and two
    async utility functions to work with it: `ping` and `stop`. Server is bound
    to host and port specified as command line arguments, e.g. `python <thefile>
    --host=127.0.0.1 --port=4242`. Server responds with the content of
    environment variable "RESPONSE" on HTTP GET request and shutdown when it
    receives HTTP POST. Function `ping` accepts two parameters `host` and
    `port`, make GET HTTP-request to the server, and returns string with request
    responce. Function `stop` also accepts `host` and `port` parameters, sends
    POST request to the server what makes it stop.

    In general the script accepts multiple ports which makes it start multiple
    servers. So you can start it as 'python <thefile> --host=127.0.0.1
    --port=4242 4444` to serve two ports `4242` and `4444`.

    Returns:
        Object with the following fields:
            script: Path to the Python file representing test HTTP server.
            ping: Callable accepting two parameters `host` and `port`, makes
                HTTP-request to the server, and returns a string with request
                responce.
            stop: Callable accepting two parameters `host` and `port` sends
                POST request to the server which makes it stop.
    """

    def http_server():
        """Source of this function is written out to a file used in tests."""
        import argparse
        import http.server
        import os
        import threading

        shutdown_requested = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                print('HTTP server received GET request')
                self.send_response(200)
                self.end_headers()
                self.wfile.write(os.environ['RESPONSE'].encode())

            def do_POST(self):
                print('HTTP server received POST request')
                self.send_response(200)
                self.end_headers()
                shutdown_requested.set()

            def log_message(self, *args, **kwargs):
                pass

        ap = argparse.ArgumentParser()
        ap.add_argument('--ports', nargs='*')
        ap.add_argument('--host')
        args = ap.parse_args()

        httpd_threads = []
        httpds = []
        for port in args.ports:
            httpd = http.server.HTTPServer(
                (str(args.host), int(port)), Handler)
            httpds.append(httpd)
            httpd_thread = threading.Thread(target=httpd.serve_forever)
            httpd_threads.append(httpd_thread)
            httpd_thread.start()
            print(('HTTP server (%s:%s) ready [thread=%s]' %
                   (args.host, port, httpd_thread.ident)))
        shutdown_requested.wait()

        for httpd in httpds:
            httpd.shutdown()

        for httpd_thread in httpd_threads:
            httpd_thread.join()
            print('Thread "%s" finished.' % httpd_thread.ident)

    script = textwrap.dedent(inspect.getsource(http_server))
    script += '\n'
    script += 'http_server()'

    filename = tmpdir_factory.mktemp('http_server').join('http_server.py')
    with open(filename, 'w') as f:
        f.write(script)
    HttpServer = collections.namedtuple(  # pylint: disable=invalid-name
        'HttpServer', ['script', 'ping', 'stop'])

    async def ping_server(host, port):
        """Make GET request to test HTTP server."""
        url = 'http://{host}:{port}'.format(host=host, port=port)
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.text()

    async def stop_server(host, port):
        """Stop test HTTP server, by sending it POST request."""
        url = 'http://{host}:{port}'.format(host=host, port=port)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url):
                    pass
        except aiohttp.ClientResponseError:
            pass

    return HttpServer(script=filename, ping=ping_server, stop=stop_server)


@pytest.fixture(scope='session')
def print_sleep_script(tmpdir_factory):
    """Prepare test program which prints given string and sleep for specified
       amount of time.

    This method returns a path to the Python script file which prints string
    given as `--print` command line argument, sleeps for number of seconds set
    by `--delay` and terminates.

    Returns:
      Path to Python script.
    """

    script = textwrap.dedent(
        '''
        import argparse
        from time import sleep
        ap = argparse.ArgumentParser()
        ap.add_argument('--delay', type=float, default=0)
        ap.add_argument('--print', default='')
        args = ap.parse_args()
        print(args.print)
        sleep(args.delay)
        '''
    )
    filename = tmpdir_factory.mktemp(
        'print_sleep_script').join('print_sleep_script.py')
    with open(filename, 'w') as f:
        f.write(script)
    return filename


@pytest.fixture(scope='session')
def print_env_script(tmpdir_factory):
    """Prepare test program which prints current environment.

    Program accepts a single command line argument which is used as to filter
    out all unrelated environment variables.

    Returns:
      Path to Python script.
    """

    script = textwrap.dedent(
        '''
        import os
        import sys
        for k,v in os.environ.items():
            if sys.argv[1] in k:
                print(k, ':', v)
        '''
    )
    filename = tmpdir_factory.mktemp('print_env').join('print_env.py')
    with open(filename, 'w') as f:
        f.write(script)
    return filename


# ------------------------------------------------------------------------- MAIN

if __name__ == '__main__':
    pytest.main(sys.argv)
