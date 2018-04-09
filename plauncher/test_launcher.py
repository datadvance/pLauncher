#
# coding: utf-8
# Copyright (c) 2018 DATADVANCE
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""
Module with tests for `Launcher` class.
"""

import asyncio
import base64
import collections
import inspect
import os
import pathlib
import random
import sys
import textwrap
import time
import uuid
import logging

import aiohttp
import psutil
import pytest

from .launcher import Launcher


# Disable warning that outer name is redefined: `pytest` dependency
# injection works this way.
# pylint: disable=redefined-outer-name


def test_main_usecase(eventloop, http_server):
    """Test main use-case.

    Test checks the following:
    - Two simple HTTP servers spawned by `Launcher`.
    - Both servers use random port number specified as callable in the
      `params` setting.
    - Substitutions like `{ServiceName_PARAM}` works in `cmd` and `env`
      settings.
    - Both servers correctly respond on the ports reported by
      `Launcher`.
    - Second process is given with dynamically determined parameter
      `port` of the first one.
    - Second server listens to two endpoints.
    - `Launcher` terminates all services when at least one of them
      stops.
    """

    # Configure two test servers.
    server1 = {
        'name': 'Server1',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
            'UNIQ': lambda: str(uuid.uuid4()),
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

        # Extract actual values of host and port from information
        # provided by launcher.
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

        # Stop one of two servers to check that second one will be
        # terminated by the launcher.
        eventloop(http_server.stop(server1_host, server1_port))

        # Wait launcher to finish, it shall finish instantly because we
        # have stopped servers already.
        eventloop(launcher.wait())
    finally:
        # Shutdown launcher.
        eventloop(launcher.shutdown())

    # Check that first server responded with our unique response.
    assert server1_resp == server1_uniq

    # Check that the second server responded with correct information
    # about the first one, hence we check that context is shared
    # properly between services.
    assert (server2_resp1 == f'{server1_host}:{server1_port}' and
            server2_resp2 == f'{server1_host}:{server1_port}')


def test_service_listens_endpoint(eventloop, http_server):
    """Test the case when process did not listen to proper endpoint, but
       it is listened by someone.

    This tests configures two servers and make `Launcher` wait second
    server to serve endpoint which it does not serve. What is more
    important is this endpoint is served by the first server. We expect
    `Launcher` to interrupt starting procedure and exit. Hence we make
    sure that `Launcher` checks not only endpoint is served, but that it
    is served by the process it just run.
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
        'endpoints_timeout': 0.1,
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


def test_service_stopped_very_fast(eventloop):
    """Test assures that if service dies immediately then it raises
       `RuntimeException`.

    This test is added to cover the case when process dies immediately
    (and very fast) after it starts. There was error when creating
    `psutil.Process` was not enclosed with try-except block (inside
    routing that process listens to the endpoint). This lead
    `Launcher.run()` to fail with `psutil.NoSuchProcess` exception and a
    lot of other errors in the log.

    To reproduce the error (until it is fixed) is important that process
    exits very fast, otherwise error is masked.
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
    """Test the case when process stops instead of starting and
       listening to the endpoint.

    Check that in such cases `Launcher` raises `RuntimeError` exception
    and stops.
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
    `logging.INFO`: capture all output and find line containing at the
    same time unique string, process id and the service name.

    Also check running `Launcher` with minimum number of settings.
    """
    unique_str = str(uuid.uuid4())

    test_service = {
        'name': 'TestService',
        'cmd': [sys.executable,
                '-u', print_sleep_script,
                '--print', unique_str],
    }

    with caplog.at_level(logging.DEBUG):
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
    """Test that `Launcher` handles the case when service child process
       listens the endpoint."""

    test_service = {
        'name':
        'TestService',
        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'UNIQ': str(uuid.uuid4()),
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

    Check that `Launcher` respects `endpoints_timeout` setting: check
    that the dummy process works at least the amount of time specified
    in this setting.
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
    # `endpoints_timeout` settings. Measure actual time until `Launcher`
    # raises `RuntimeError` exception.
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

    # Check that the difference between these time periods is
    # significant - greater than at least half of timeouts difference.
    assert elapsed_2 - elapsed_1 > (TIMEOUT_2 - TIMEOUT_1) / 2


def test_process_exit(eventloop, http_server, print_sleep_script):
    """Test shutdown procedure when one of services terminates."""

    # Configure several HTTP servers and add service, which terminates
    # in few seconds.
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
    - Values from `env` setting is available in the process for both
      new and modified variables.
    - Setting some of `env` values to `None` removes it from
      environment.
    """

    # Setup three environment variables with globally unique names and
    # values: the first shall pass as is; value of the second will be
    # modified; the third will be removed.

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
    with caplog.at_level(logging.DEBUG):
        launcher = Launcher([env_printer])
        try:
            eventloop(launcher.run())
            eventloop(launcher.wait())
        finally:
            eventloop(launcher.shutdown())

    # Check that name and value of first variable exists in output.
    # Since we rely on global uniqueness of variable names and values we
    # can simply check their existence as substring.
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

    Generate globally unique value and put it into environment. Run test
    script which prints environment and make `Launcher` wait until it
    starts serving endpoint. Since script will not do this, `Launcher`
    must try to restart the script several times. To check the number of
    times `Launcher` does this we count number of occurrences of the
    unique values in the output.
    """

    # Generate globally unique value.
    uniq = base64.b32encode(uuid.uuid4().bytes).replace(
        b'=', b'').decode().lower()

    with caplog.at_level(logging.DEBUG):
        experiments = [1, 2, 4]
        for attempts in experiments:
            env_printer = {
                'name': 'EnvPrinter',
                'env': {'VARIABLE': uniq},
                'endpoints': [('1.2.3.4', 42)],
                'endpoints_timeout': sys.float_info.epsilon,
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

    # Check that unique value appears exactly proper number of times in
    # the test output.
    assert str(caplog.text).count(uniq) == sum(experiments), (
        'wrong number of attempts to run service'
    )


def test_launcher_name(eventloop, caplog):
    """Check that `Launcher` uses `name` argument as logger name"""

    unique_str = str(uuid.uuid4())

    test_service = {
        'name': 'TestService',
        'cmd': [sys.executable, '-c', 'pass'],
    }

    with caplog.at_level(logging.DEBUG):
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

    # Check wrong value: wait
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'wait': 'somestr'}])

    # Check wrong value: wait_timeout
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'wait_timeout': -1}])
    check_fails([{'name': 'NoOp', 'cmd': noop_cmd, 'wait_timeout': 'somestr'}])


def test_files(eventloop, tmpdir, print_file_script, caplog):
    """Check file processing.

    Test makes sure that:
    - specified files created with proper content;
    - parameter substitutions works in the file body and the file name;
    - name of created file corresponds to the key in the `files`
      dictionary;
    - temporary files are removed when service finishes;
    - temporary files are removed when service fails.
    """

    uniq1 = str(uuid.uuid4())
    uniq2 = str(uuid.uuid4())
    filename1 = str(pathlib.Path(tmpdir) / str(uuid.uuid4()))
    filename2 = str(pathlib.Path(tmpdir) / str(uuid.uuid4()))
    file_printer = {
        'name': 'Service',
        'params': {
            'UNIQ1': uniq1,
            'UNIQ2': uniq2,
            'FILENAME1': filename1,
            'FILENAME2': filename2,
        },
        'files': {
            '{Service_FILENAME1}': '{Service_UNIQ1}',
            '{Service_FILENAME2}': '{Service_UNIQ2}',
        },
        'cmd': [
            sys.executable, '-u', print_file_script,
            '{Service_FILENAME1}', '{Service_FILENAME2}'
        ],
    }

    # Run the launcher and capture all logs.
    with caplog.at_level(logging.DEBUG):
        launcher = Launcher([file_printer])
        try:
            eventloop(launcher.run())
            eventloop(launcher.wait())
        finally:
            eventloop(launcher.shutdown())

    # Check written out file context.
    assert uniq1 in caplog.text, 'file not found or context is wrong'
    assert uniq2 in caplog.text, 'file not found or context is wrong'
    # Make sure temporary files removed.
    assert not os.path.exists(filename1), 'file is not removed'
    assert not os.path.exists(filename2), 'file is not removed'
    assert filename1 in launcher.services['Service'].files
    assert filename2 in launcher.services['Service'].files

    # Check that file removed when service fails.
    filename = str(pathlib.Path(tmpdir) / str(uuid.uuid4()))
    fail_service = {
        'name': 'Service',
        'params': {'FILENAME': filename},
        'endpoints': [('1.2.3.4', 42)],
        'endpoints_timeout': sys.float_info.epsilon,
        'files': {'{Service_FILENAME}': ''},
        'cmd': [sys.executable, '-u', 'pass'],
    }
    launcher = Launcher([fail_service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())
    assert not os.path.exists(filename), ('file is not removed when service '
                                          'fails')


def test_files_dirs_workdir(eventloop, tmpdir, print_file_script, caplog):
    """Check `dirs` and `workdir` settings.

    Test makes sure that:
    - Working directory of service is set up correctly.
    - Is file specified without path it is created in the service
      working directory.
    - Additional directory is created from a prototype.
    - Additional directory is created without prototype.
    - Additional directories are removed when service stops.
    - Additional directories are removed when service fails.
    """

    # There will be three files:
    # - the first relies in the prototype directory, which is also set
    #   as the working directory of the service;
    # - the second is created in the working directory by `Launcher`
    #   using `files` setting;
    # - the third is created again by the Launcher, but in the another
    #   directory.

    # Prepare paths.
    protodir = pathlib.Path(tmpdir) / 'protodir'
    filename1 = str(uuid.uuid4())
    filename2 = str(uuid.uuid4())
    filename3 = str(uuid.uuid4())
    content1 = str(uuid.uuid4())
    content2 = str(uuid.uuid4())
    content3 = str(uuid.uuid4())
    workdir = os.path.join(tmpdir, str(uuid.uuid4()))
    somedir = os.path.join(tmpdir, str(uuid.uuid4()))

    # Create a prototype directory with a file inside.
    os.makedirs(protodir)
    with open(protodir / filename1, 'w') as f:
        f.write(content1)

    # Configure service.
    service = {
        'name': 'Service',
        'params': {
            'WORKDIR': workdir,
            'SOMEDIR': somedir,
            'FILENAME1': filename1,
            'FILENAME2': filename2,
            'FILEPATH3': '{Service_SOMEDIR}' + os.path.sep + filename3,
        },
        'dirs': {
            '{Service_WORKDIR}': protodir,
            '{Service_SOMEDIR}': None,
        },
        'files': {
            '{Service_FILENAME2}': content2,
            '{Service_FILEPATH3}': content3,
        },
        'workdir': '{Service_WORKDIR}',
        'cmd': [
            sys.executable, '-u',
            print_file_script,
            '{Service_FILENAME1}', '{Service_FILENAME2}', '{Service_FILEPATH3}'
        ],
    }

    with caplog.at_level(logging.DEBUG):
        launcher = Launcher([service])
        try:
            eventloop(launcher.run())
            eventloop(launcher.wait())
        finally:
            eventloop(launcher.shutdown())

    assert filename1 in caplog.text
    assert filename2 in caplog.text
    assert filename3 in caplog.text
    assert content1 in caplog.text
    assert content2 in caplog.text
    assert content3 in caplog.text

    assert not os.path.exists(workdir), ('Directory created by `dirs` is not '
                                         'removed!')
    assert not os.path.exists(somedir), ('Directory created by `dirs` is not '
                                         'removed!')

    # Update configuration to make service fail on start and check that
    # directories are removed.
    service.update({
        'endpoints': [('1.2.3.4', 42)],
        'endpoints_timeout': sys.float_info.epsilon,
    })
    launcher = Launcher([service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())
    assert not os.path.exists(workdir), ('Directory created by `dirs` is not '
                                         'removed when service fails!')
    assert not os.path.exists(somedir), ('Directory created by `dirs` is not '
                                         'removed when service fails!')


def test_wait_timeout(eventloop, caplog, print_sleep_script):
    """Check settings `wait` and `wait_timeout`.

    - Run two processes. The first one with `wait` set to `True`. Make
      sure that when second process starts the first one has already
      finished.
    - Run endless process with `wait` and `wait_timeout` set. Make sure
      `Launcher` finishes with error.
    """

    # Run two processes. Second one checks that the first one is not
    # running. We use unique marker in the command line to check the
    # process existence. Unique flags are printed out for checks.

    proc1_flag = str(uuid.uuid4())
    proc2_flag = str(uuid.uuid4())
    proc2_do_not_see_proc1_flag = str(uuid.uuid4())
    services = [
        {
            'name': 'Process1',
            'cmd': [sys.executable, '-u',
                    print_sleep_script, '--delay=0.5', '--print', proc1_flag],
            'wait': True,
        },
        {
            'name': 'Process2',
            'cmd': [
                sys.executable, '-c',
                textwrap.dedent(f'''
                    print('{proc2_flag}')
                    from psutil import *;
                    print([p.cmdline() for p in process_iter()
                                         if p.pid != Process().pid])
                    if ('{proc1_flag}' not in
                        str([p.cmdline() for p in process_iter()
                                         if p.pid != Process().pid])):
                        print('{proc2_do_not_see_proc1_flag}'*2)
                ''')
            ]
        },
    ]

    with caplog.at_level(logging.DEBUG):
        launcher = Launcher(services)
        try:
            eventloop(launcher.run())
            eventloop(launcher.wait())
        finally:
            eventloop(launcher.shutdown())

    messages = [r[2] for r in caplog.record_tuples]
    assert proc1_flag in messages, 'The first process did not run!'
    assert proc2_flag in messages, 'The second process did not run!'
    assert proc2_do_not_see_proc1_flag * 2 in caplog.text, (
        'Second process detected the first one, `wait` did not work!'
    )

    # Run endless process with `wait` and `wait_timeout` set. Make sure
    # `Launcher` finishes with error.

    service = {
        'name': 'EndlessProcess',
        'cmd': [sys.executable, '-u', print_sleep_script, '--delay=100500'],
        'wait': True,
        'wait_timeout': 0.1,
    }

    launcher = Launcher([service])
    try:
        pytest.raises(RuntimeError, eventloop, launcher.run())
        eventloop(launcher.wait())
    finally:
        eventloop(launcher.shutdown())

# ------------------------------------------------------------ UTILITY FIXTURES


@pytest.fixture(scope='function')
def eventloop():
    """Fixture provides eventloop to tests and perform cleanup checks.

    Fixture provides eventloop to tests and check that test did not
    leave child processes or pending tasks. It yields callable which
    accepts coroutine or future and wait until it completes. It is
    needed to `await` in synchronous code.

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

    # Check there is no child processes left behind when test function
    # finishes, cleanup event loop.

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

    This method returns a path to Python script with simple HTTP server
    and two async utility functions to work with it: `ping` and `stop`.
    Server is bound to host and port specified as command line
    arguments, e.g. `python <thefile> --host=127.0.0.1 --port=4242`.
    Server responds with the content of environment variable "RESPONSE"
    on HTTP GET request and shutdown when it receives HTTP POST.
    Function `ping` accepts two parameters `host` and `port`, make GET
    HTTP-request to the server, and returns string with request
    response. Function `stop` also accepts `host` and `port` parameters,
    sends POST request to the server what makes it stop.

    In general the script accepts multiple ports which makes it start
    multiple servers. So you can start it as 'python <thefile>
    --host=127.0.0.1 --port=4242 4444` to serve two ports `4242` and
    `4444`.

    Returns:
        Object with the following fields:
            script: Path to the Python file representing test HTTP
                server.
            ping: Callable accepting two parameters `host` and `port`,
                makes HTTP-request to the server, and returns a string
                with request response.
            stop: Callable accepting two parameters `host` and `port`
                sends POST request to the server which makes it stop.
    """

    def http_server():
        """Source of the function writes out to a file for tests."""
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
        url = f'http://{host}:{port}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.text()

    async def stop_server(host, port):
        """Stop test HTTP server, by sending it POST request."""
        url = f'http://{host}:{port}'
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url):
                    pass
        except aiohttp.ClientResponseError:
            pass

    return HttpServer(script=filename, ping=ping_server, stop=stop_server)


@pytest.fixture(scope='session')
def print_sleep_script(tmpdir_factory):
    """Prepare test program which prints given string and sleep for
       specified amount of time.

    This method returns a path to the Python script file which prints
    string given as `--print` command line argument, sleeps for number
    of seconds set by `--delay` and terminates.

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

    Program accepts a single command line argument which is used as to
    filter out all unrelated environment variables.

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


@pytest.fixture(scope='session')
def print_file_script(tmpdir_factory):
    """Prepare script which prints files specified in the command line.

    Script treats command line arguments as filenames of a files to
    output to `stdout`. The output is simple: `filename: file content`.

    Returns:
        Path to Python script.
    """

    script = textwrap.dedent('''
        import sys
        for filename in sys.argv[1:]:
            with open(filename) as f:
                print(f'{filename}:', f.read())
    ''')
    filename = tmpdir_factory.mktemp('print_file').join('print_file.py')
    with open(filename, 'w') as f:
        f.write(script)
    return filename

# ------------------------------------------------------------------------ MAIN


if __name__ == '__main__':
    pytest.main(sys.argv)
