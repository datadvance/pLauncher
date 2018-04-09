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
Module provides `Launcher` class spawning and watching over services.
"""

import asyncio
import collections
import copy
import logging
import os
import shutil
import sys
import time
import types

import psutil


class Launcher(object):
    """Launcher spawns group of services and watch over them.

    Features:
      - Declarative configuration.
      - Allow services to have dynamic configuration: service may start
        with dynamically selected parameter value. It is developed in
        order to provide ability to start service on randomly selected
        port.
      - Allow services to have shared context. For example when one
        service starts on randomly selected port, configuration of
        another service may refer to it.

    Launcher starts services one by one using the `config` given to the
    constructor. Configuration is the list of dicts. The following
    settings are available:
        name: The name of service. Used for logging and as prefix for
            substitutions: `{ServiceName_PARAM}`. Name must be unique.
            Required.
        params: Parameters mapping. Keys are strings which are used in
            substitutions. Values can be of any type and used as
            `str(value)`, but if the value is callable it is invoked:
            used as `str(value())`. Callables in param values are rerun
            on each attempt. This allows to have dynamic configuration.
            Substitutions can be used in values, this allows to make
            parameter value dependent on parameter of another service.
            The substitution syntax is the following
            `{ServiceName_PARAM_NAME}` and if some value already
            contains curly braces they need to be doubled to avoid
            syntax errors. Optional.
        files: Dictionary with names and context of files to be written
            out when service starts. Each key is a filename of a file to
            be created and a value is a string which will be written to
            that file. A motivation for this setting is that some
            programs (like Nginx) cannot get the configuration from a
            command line parameters or environment variables and
            require it to be written out to a file. It is important to
            note that you can use parameter substitutions inside both:
            file name and file content (keys and values in this `files`
            setting). This allows filename to be specified inside
            `params` section and used everywhere just like any other
            parameter value. Also note, that files are recreated on each
            attempt to start the service, this allows to used updated
            parameter values when parameters are specified as callables.
            When key is not a full path then file is created in the
            service working directory. Optional.
        dirs: Dictionary with directories which need to be created
            before service starts. Each key stands for the absolute path
            of the directory going to be created and and a value is a
            path to another directory which content is used as a
            prototype (which content is copied recursively). If value is
            `None` then just new empty directory is created. Like with
            `files` you can use parameter substitutions in both keys and
            values. Directories are removed when service stops.
            Optional.
        endpoints: Make sure service (its process or child process)
            serves given endpoints. Endpoints specified as a list of
            tuples with host and port. E.g. `[('127.0.0.1','42')]`.
            Substitutions can be used, which allows to check dynamically
            selected endpoints: `[('127.0.0.1', '{ServiceName_PORT}')]`
            will replace `{ServiceName_PORT}` with the parameter named
            `PORT`. Optional.
        endpoints_timeout: The time period while `Launcher` waits for a
            service to start listening to the `endpoints`. If the
            service has not started listening at the moment when timeout
            expires, it is considered that it failed to start. Optional.
            Default is 7 seconds.
        wait: If set to `True` then `Launcher` stop running services
            until this process finishes. Optional. Default is `False`.
        wait_timeout: The time period while `Launcher` waits the process
            to finish. If the process has not finished at the moment
            when timeout expires, it is considered that it has failed.
            Optional. Default is 7 seconds.
        attempts: The number of attempts to start service. Service is
            not started if it does not serve endpoint specified by
            `endpoints`. Default is 3.
        env: Mapping specifying environment variables. Substitutions can
            be used. Process receives parent's environment with some
            variables added in this collection. If value is `None` when
            variable is removed from the environment. Optional.
        workdir: Working directory of the service. Default is `None`
            which means that current working directory is used.
            Optional.
        cmd: List with executable and command line arguments.
            Substitutions can be used. Required.

    Example:
      ```
      # Worker is started on random port.
      worker_cfg = {
          'name': 'Worker',

          'params': {
              'PORT': lambda: random.randint(49152, 65535),
              'HOST': '127.0.0.1',
              'PATH': '/home/user',
          },

          'endpoints': [('{Worker_HOST}', '{Worker_PORT}')],
          'attempts': 5,

          'env': {'PATH': '{Worker_PATH}'}
          'cmd': ['./worker', '--config', '{Worker_CONFIG}',
                  '--host={Worker_HOST}', '--port={Worker_PORT}'],
      }

      server_cfg = {
          'name': 'Server',

          'params' : {
              'CONFIG': '/home/users/server.cfg',
          }

          'files': {
              '{CONFIG}': '''{{"service_port": "{Worker_PORT}"}}''',
          },

          'endpoints': [('127.0.0.1', '8080')],
          'attempts': 3,

          'cmd': ['./server',
                  '--worker-host={Worker_HOST}',
                  '--worker-port={Worker_PORT}',
                  '--config={Server_CONFIG}'],
      }

      launcher = Launcher([worker_cfg, server_cfg])
      try:
          await launcher.run()
          await launcher.wait()
      finally:
          await launcher.shutdown()
      ```
    """

    # Sleep between attempts to check endpoint.
    ENDPOINT_SLEEP_TIME = 0.02
    # Total time to check for endpoint.
    ENDPOINTS_TIMEOUT = 7
    # Time to wait until process (with `wait` set to `True`) finishes.
    WAIT_TIMEOUT = 7
    # Process termination timeout.
    TERMINATION_TIMEOUT = 5
    # Child processes collection update period.
    CHILDREN_UPDATE_PERIOD = 0.1
    # Default of `attempts` setting.
    ATTEMPTS_DEFAULT = 3

    # Service information structure (after applying parameter
    # substitutions).
    ServiceInfo = collections.namedtuple(
        'ServiceInfo',
        [
            'name',     # service name
            'config',   # copy of service configuration
            'params',   # parameter values
            'files',    # files
            'dirs',     # direcotires
            'env',      # environment variables
            'workdir',  # working directory
            'cmd',      # command line
            'process',  # instance of `asyncio.subprocess.Process`
            'monitor',  # asyncio task responsible for stream
                        # redirection and process monitoring
        ],
    )

    def __init__(self, configs, name=None):
        """Constructs `Launcher` instance.

        Args:
            configs: List of service configurations.
            name: Name of launcher instance, used for logging.
        Raises:
            AssertionError, ValueError, TypeError, IndexError:
                Configuration contain mistakes.
        """

        # Validate and copy services configuration. This may raise
        # `AssertionError` exception.
        self._configs = self._validate_and_copy_configs(configs)

        # The context with service parameters. This holds all the
        # information shared between services: port numbers, hosts
        # services are bound to, etc.
        self._context = {}

        # List of running services. Contains all the information about
        # each service.
        self._services = collections.OrderedDict()

        # Launcher logger.
        self._logger = logging.getLogger(
            name if name is not None else Launcher.__name__
        )

        # Here we collect all the child processes we have ever seen.
        self._children = set()

        # Triggering this event will initiate shutdown procedure.
        self._shutdown = asyncio.Event()

        # Task which continuously update child processes list.
        self._children_monitor_task = None

    async def run(self):
        """Run services.

        Start all services and return dictionary with service details.

        Raises:
            RuntimeError: Service start procedure is failed by some
                reason: process did not start serving `endpoints`.
                Exception is raised after `attempts` number of such
                faults.
        """

        # Start coroutine which continuously updates child processes
        # list. This is needed in order to kill all spawned process on
        # exit.
        self._children_monitor_task = asyncio.ensure_future(
            self._children_monitor()
        )

        for cfg in self._configs:
            # Service name.
            name = cfg.name

            for attempt in range(cfg.attempts):

                self._logger.info('Starting %s (attempt %s)', name, attempt)

                # Get parameters and put their values into the context.
                params = collections.OrderedDict()
                for param_name, param_value in cfg.params.items():
                    # run parameter if it is callable
                    if callable(param_value):
                        param_value = str(param_value())

                    # enrich value with current context, for example
                    # values from other services
                    param_value = param_value.format(**self._context)
                    # put new parameters to the context
                    self._context['%s_%s' % (name, param_name)] = param_value
                    # store the parameter for the service info
                    params[param_name] = param_value

                # Prepare environment: enrich variable values given in
                # `env` setting with the context and put it into the
                # system environment, overwriting any existing
                # variables. If value of given variable is `None` then
                # such variable is removed from environment, if there is
                # one.
                env = dict(os.environ)
                for var_name, var_value in cfg.env.items():
                    if var_value is not None:
                        env[var_name] = var_value.format(**self._context)
                    elif var_name in env:
                        del env[var_name]

                # Prepare directories: create directories described in
                # the `dirs` section enriching both: a directory path
                # and prototype directory path with values from the
                # context.
                dirs = dict()
                for dir_path, protodir_path in cfg.dirs.items():
                    dir_path = dir_path.format(**self._context)
                    assert os.path.isabs(dir_path), (
                        f'Path `{dir_path}` is relative, absolute paths '
                        'expected in keys of the `dirs` setting!'
                    )
                    if protodir_path is not None:
                        protodir_path = protodir_path.format(**self._context)
                        shutil.copytree(protodir_path, dir_path)
                    else:
                        os.makedirs(dir_path)
                    dirs[dir_path] = protodir_path

                # Working directory.
                workdir = cfg.workdir
                if workdir is not None:
                    workdir = workdir.format(**self._context)

                # Prepare files: create files described in the `files`
                # section enriching both: a filename and a content of
                # the file with values from the context.
                files = dict()
                for filename, file_content in cfg.files.items():
                    filename = filename.format(**self._context)
                    if not os.path.isabs(filename) and workdir is not None:
                        filename = os.path.join(workdir, filename)
                    file_content = file_content.format(**self._context)
                    with open(filename, mode='w') as f:
                        f.write(file_content)
                    files[filename] = file_content

                # Get command line and enrich it with the context.
                cmd = [s.format(**self._context) for s in cfg.cmd]

                # Spawn service process.
                self._logger.info('Exec: %s', ' '.join(cmd))
                process = await asyncio.create_subprocess_exec(
                    *cmd, env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=workdir,
                )

                # Start process monitor which redirects outputs to
                # logging.
                process_monitor = asyncio.ensure_future(
                    self._process_monitor(name, process)
                )

                # Check that process listens required endpoints, kill it
                # otherwise.
                endpoint_checkers = []
                for endpoint in cfg.endpoints:
                    host = endpoint[0].format(**self._context)
                    port = int(endpoint[1].format(**self._context))
                    endpoint_checkers.append(
                        self._service_listens_endpoint(name, process,
                                                       host, port,
                                                       cfg.endpoints_timeout)
                    )
                if endpoint_checkers:
                    completed_checks, _ = await asyncio.wait(endpoint_checkers)
                    if all(map(lambda _: _.result(), completed_checks)):
                        self._logger.info(
                            '%s is listening endpoint(s): %s',
                            name, str(cfg.endpoints).format(**self._context)
                        )
                    else:
                        self._logger.warning(
                            '%s does not listen to endpoint(s): %s',
                            name, str(cfg.endpoints).format(**self._context)
                        )
                        # Kill the process and remove files and dirs.
                        await self._terminate_process(name, process)
                        for filename in files:
                            os.remove(filename)
                        for dir_path in dirs:
                            shutil.rmtree(dir_path)
                        # Proceed to the next attempt.
                        continue

                # When `wait` set to `True` the process must finish
                # before other services run.
                if cfg.wait:
                    try:
                        self._logger.info(
                            'Wait %s[%s] to finish in %s seconds.',
                            name, process.pid, cfg.wait_timeout
                        )
                        exitcode = await asyncio.wait_for(
                            process.wait(), timeout=cfg.wait_timeout
                        )
                        self._logger.info(
                            'Process %s[%s] finished with exit code %s.',
                            name, process.pid, exitcode
                        )
                        # Process has finished so proceed to the next
                        # service.
                        break
                    except asyncio.TimeoutError:
                        self._logger.error(
                            'Process %s[%s] did not stop in %s secs!',
                            name, process.pid, cfg.wait_timeout
                        )
                        # Kill the process.
                        await self._terminate_process(name, process)
                        # Proceed to the next attempt.
                        continue
                    finally:
                        # Cleanup: remove files and dirs.
                        for filename in files:
                            os.remove(filename)
                        for dir_path in dirs:
                            shutil.rmtree(dir_path)

                self._logger.info('%s started (attempt %s)', name, attempt)

                # Service monitor wraps process monitor and fire shutdown
                # signal when process finishes.
                service_monitor = asyncio.ensure_future(
                    self._service_monitor(name, process_monitor)
                )

                # Build and remember service info.
                service_info = self.ServiceInfo(
                    name=name,
                    config=copy.deepcopy(cfg),
                    params=params,
                    files=files,
                    dirs=dirs,
                    env=env,
                    workdir=workdir,
                    cmd=cmd,
                    process=process,
                    monitor=service_monitor
                )
                self._services[name] = service_info
                break
            else:
                # Set shutdown event to avoid deadlock if client code
                # invokes `wait` method.
                self._shutdown.set()
                raise RuntimeError(f'Failed to start {name}: '
                                   f'{attempt} attempts made!')

        return self._services

    async def wait(self):
        """Wait until shutdown process started."""
        await self._shutdown.wait()

    async def shutdown(self):
        """Shutdown launcher: destroy all childrenand stop async tasks."""

        self._logger.info('Shutdown sequence initiated')

        # Turn off children monitor.
        self._children_monitor_task.cancel()
        await asyncio.wait([self._children_monitor_task])

        # Terminate services in the order reverse to the creation order.
        for name, service_info in reversed(self._services.items()):
            await self._terminate_process(name, service_info.process)
            # cancel process monitor
            service_info.monitor.cancel()
            await asyncio.wait([service_info.monitor])

        # Just in case, extend list of known children with current
        # children set. This could help if some nimble child made new
        # children during its lifetime (children are growing fast,
        # right).
        await self._update_children()

        # Terminate all the child processes, including ones we did not
        # fork directly.
        await self._exterminate_children()

        # Remove files and directories created to run services.
        for service_info in self._services.values():
            for filename in service_info.files:
                os.remove(filename)
            for dir_path in service_info.dirs:
                shutil.rmtree(dir_path)

    @property
    def services(self):
        """Dictionary with information about services."""
        return self._services

    @classmethod
    def _validate_and_copy_configs(cls, configs):
        """Validate configuration and return deep copy of it.

        Raises:
            AssertionError, ValueError, TypeError, IndexError:
                Configuration contain mistakes.

        Returns:
            Validated configuration where values are already casted to
            proper types.
        """
        result = []
        names = set()
        for config in configs:
            result += [types.SimpleNamespace()]
            cfg = result[-1]

            # Default values.
            cfg.attempts = cls.ATTEMPTS_DEFAULT
            cfg.params = collections.OrderedDict()
            cfg.files = {}
            cfg.dirs = {}
            cfg.env = {}
            cfg.endpoints = []
            cfg.endpoints_timeout = cls.ENDPOINTS_TIMEOUT
            cfg.wait = False
            cfg.wait_timeout = cls.WAIT_TIMEOUT
            cfg.workdir = None

            for setting in config:

                if setting == 'name':
                    cfg.name = str(config[setting])
                    assert cfg.name not in names, (
                        f'Duplicate service name: {cfg.name}!'
                    )
                    names.add(cfg.name)
                elif setting == 'params':
                    for k, v in config[setting].items():
                        cfg.params[str(k).strip()] = (
                            str(v) if not callable(v) else v
                        )
                    assert '' not in cfg.env, 'Empty parameter name!'
                elif setting == 'files':
                    cfg.files = {
                        str(k).strip(): str(v)
                        for k, v in dict(config[setting]).items()
                    }
                elif setting == 'dirs':
                    cfg.dirs = {
                        str(k).strip(): str(v) if v is not None else None
                        for k, v in dict(config[setting]).items()
                    }
                elif setting == 'endpoints':
                    assert not isinstance(config[setting], str), (
                        'Setting `endpoints` is str, list of tuples expected!'
                    )
                    for endpoint in config[setting]:
                        host = str(endpoint[0]).strip()
                        port = str(endpoint[1]).strip()
                        assert host, 'Endpoint `host` is empty!'
                        assert port, 'Endpoint `port` is empty!'
                        cfg.endpoints += [(host, port)]
                elif setting == 'endpoints_timeout':
                    cfg.endpoints_timeout = float(config[setting])
                    assert cfg.endpoints_timeout > 0, (
                        'Endpoint timeout is less than or equal to zero!'
                    )
                elif setting == 'wait':
                    assert isinstance(config[setting], bool), (
                        'Wait flag is not of the `bool` type!'
                    )
                    cfg.wait = bool(config[setting])
                elif setting == 'wait_timeout':
                    cfg.wait_timeout = float(config[setting])
                    assert cfg.wait_timeout > 0, (
                        'Wait timeout is less than or equal to zero!'
                    )
                elif setting == 'attempts':
                    attempts = int(config[setting])
                    assert attempts > 0, (
                        'Start attempts number is less than or equal to zero!'
                    )
                    cfg.attempts = attempts
                elif setting == 'env':
                    cfg.env = {
                        str(k).strip(): str(v) if v is not None else v
                        for k, v in dict(config[setting]).items()
                    }
                    assert '' not in cfg.env, ('Empty environment variable '
                                               'name!')
                elif setting == 'workdir':
                    cfg.workdir = str(config[setting])
                elif setting == 'cmd':
                    assert not isinstance(config[setting], str), (
                        'Setting `cmd` is str, list expected!'
                    )
                    cfg.cmd = [str(item) for item in config[setting]]
                    assert cfg.cmd, 'Empty command line given!'
                else:
                    assert False, f'Unknown service setting: `{setting}`!'

            assert hasattr(cfg, 'name'), 'Mandatory setting absent: `name`!'
            assert hasattr(cfg, 'cmd'), 'Mandatory setting absent: `cmd`!'

        return result

    async def _service_listens_endpoint(self, name, process,
                                        host, port, timeout):
        """Check that service listens on some network endpoint.

        Method checks that service process specified by
        `asyncio.subprocess.Process` instance one of its children
        listens on the network endpoint specified by `host` and `port`.
        Algorithm checks this every `self.ENDPOINT_SLEEP_TIME` seconds,
        until either process starts listening or the `timeout` expires.

        Args:
            name: Service name, used for logging.
            process: Instance of `asyncio.subprocess.Process` to check.
            host: String with host, e.g. '127.0.0.1'.
            port: Port number.

        Returns:
            `True` if process (or one of its children) is listening,
            `False` otherwise.
        """
        self._logger.debug('Waiting %s to listen on %s:%s', name, host, port)
        try:
            proc = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            self._logger.debug(
                'Service %s[%s] process no longer exists', name, process.pid
            )
            # service is dead and does not listen anything
            return False

        port_listener_found = False
        process_terminated = False
        start_time = time.monotonic()
        while not port_listener_found:
            try:
                # Take connections of the process and its children.
                connections = proc.connections()
                children = proc.children(recursive=True)
                for child in children:
                    try:
                        connections += child.connections()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        # we do not care if some grandchild process
                        # terminates, not our business
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # If process terminates then wait until
                # `asyncio.subprocess.Process` instance knows this. This
                # allows process monitors to handle output streams
                # correctly.
                #
                # Exception `psutil.AccessDenied` is caught as well
                # because it may be thrown by `proc.connections()` when
                # process disappears.
                try:
                    await asyncio.wait([process.wait()],
                                       timeout=self.TERMINATION_TIMEOUT)
                except asyncio.TimeoutError:
                    assert False, 'Timeout waiting for the terminated process!'
                process_terminated = True
                break
            else:
                endpoints = [conn.laddr for conn in connections
                             if conn.status == psutil.CONN_LISTEN]
                port_listener_found = any(
                    [endpoint == (host, port) for endpoint in endpoints]
                )
                if (not port_listener_found and
                        time.monotonic() - start_time > timeout):
                    break
                elif not port_listener_found:
                    # give the process some time to start
                    await asyncio.sleep(self.ENDPOINT_SLEEP_TIME)

        if process_terminated:
            self._logger.debug(
                '%s disappeared instead of listening endpoint', name
            )
            return False
        elif not port_listener_found:
            self._logger.debug(
                '%s did not start listening %s:%s during %s seconds',
                name, host, port, timeout
            )
            return False

        return True

    async def _terminate_process(self, name, process):
        """Terminate process by given `asyncio.subprocess.Process` instance.

        Args:
          name: String with service name, used for logging.
          process: Instance of `asyncio.subprocess.Process` to terminate.
        """

        # Collect targets: process itself and its children.
        targets = set()
        try:
            proc = psutil.Process(process.pid)
            targets.update([proc])
            targets.update(proc.children(recursive=True))
        except psutil.NoSuchProcess:
            # Wait until `asyncio.subprocess.Process` instance knows
            # that process has terminated. This allows process monitors
            # to handle output streams correctly.
            await asyncio.wait([process.wait()])
            self._logger.info('%s[%s] has just disappeared.',
                              name, process.pid)
            return

        # Start with gentle asyncio, hoping it will terminate its
        # children.
        try:
            self._logger.info('Terminating %s[%s]...', name, process.pid)
            process.terminate()
            self._logger.info('...terminate signal sent to %s[%s]...',
                              name, process.pid)
            exitcode = await asyncio.wait_for(process.wait(),
                                              timeout=self.TERMINATION_TIMEOUT)
            self._logger.info('...process %s[%s] finished with exit code %s.',
                              name, process.pid, exitcode)
        except ProcessLookupError:
            self._logger.info('...service %s[%s] has just disappeared.',
                              name, process.pid)
        except asyncio.TimeoutError:
            self._logger.warning(
                '%s[%s] did not stop in %s secs!',
                name, process.pid, self.TERMINATION_TIMEOUT
            )

        # Just in case use nuclear weapon from psutil to exterminate it
        # with children.
        self._exterminate_processes(targets)

    async def _service_monitor(self, name, process_monitor):
        """Service monitor: wait given process monitor to finish then
           initiate shutdown procedure."""
        await asyncio.wait([process_monitor])
        self._logger.info('%s stopped', name)
        self._shutdown.set()

    async def _process_monitor(self, name, process):
        """Process monitor: redirect process output to logging, wait it
           to finish and cleanup."""

        async def stream_logger(stream):
            """Auxiliary coroutine to redirect stream to logger."""
            try:
                logger = logging.getLogger('{}[{}]'.format(name, process.pid))
                while not stream.at_eof():
                    try:
                        # Use `readuntil` instead of `readline` because
                        # it allows to handle very long strings manually
                        # by processing exceptions. At the moment of
                        # writing, `readline` simply raises `ValueError`
                        # in case of very long line.
                        line = await stream.readuntil(
                            separator='\n'.encode(sys.stdout.encoding)
                        )

                    # Line is too long to fit into buffer.
                    except asyncio.streams.LimitOverrunError as e:
                        # Read what we can read.
                        line = await stream.read(e.consumed)
                        line += b'...'

                    # EOF occurred.
                    except asyncio.streams.IncompleteReadError as e:
                        # Get what has already been read.
                        line = e.partial

                    # Do not report empty lines, proper line contains at
                    # least line ending. Completely empty lines may
                    # occur at the EOF.
                    if line:
                        # Use `stdout.encoding` to handle non-ascii
                        # chars and strip separator from the end of the
                        # line.
                        logger.log(
                            logging.INFO,
                            line.decode(sys.stdout.encoding).rstrip()
                        )

            # It is OK when coroutine is interrupted. For example that
            # is what happenes when user press <Control+C> to stop the
            # process.
            except asyncio.CancelledError:
                raise

            # Catch all the exception and report about them.
            except Exception as e:  # pylint: disable=broad-except
                self._logger.error('Logger %s is out of order: %s %s.',
                                   '{}[{}]'.format(name, process.pid),
                                   type(e), e)
            finally:
                self._logger.debug('Redirection to the logger %s stopped.',
                                   '{}[{}]'.format(name, process.pid))

        # Start tasks serving process standard output streams.
        stdout_logger_task = asyncio.ensure_future(
            stream_logger(process.stdout)
        )
        stderr_logger_task = asyncio.ensure_future(
            stream_logger(process.stderr)
        )

        # Wait process to terminate.
        exitcode = await process.wait()

        # Cleanup logging tasks.
        stdout_logger_task.cancel()
        stderr_logger_task.cancel()
        await asyncio.wait([stdout_logger_task, stderr_logger_task])

        return exitcode

    async def _children_monitor(self):
        """Continuously update collection of known children.

        In order to cleanup correctly we need to remember all the child
        processes we spawn, both directly and indirectly. It is
        necessary to count all the children while they are alive,
        otherwise when some intermediate process dies we lose their
        children. Like in the picture below, where if X dies then Y will
        be lost.
          A ─┐
             ├─ B (child) ─┐
             │             └─ X (grandchild) ─┐
             │                                └─ Y (great grandchild)
             └─ C (child)
        """
        while True:
            await self._update_children()
            await asyncio.sleep(self.CHILDREN_UPDATE_PERIOD)

    async def _update_children(self):
        """Update list of known child processes: add new children (if
           found) and remove terminated."""

        # Gather all current child processes.
        children = psutil.Process().children(recursive=True)

        # Report new children.
        new_children = set(children).difference(self._children)
        if new_children:
            self._logger.debug(
                'New child processes detected: %s',
                ['%s[%s]' % (new_child.name(), new_child.pid)
                 for new_child in new_children]
            )

        # Update children collection and filter out terminated
        # processes.
        self._children.update(children)
        for child in set(self._children):
            try:
                if child.status() not in [psutil.STATUS_STOPPED,
                                          psutil.STATUS_DEAD]:
                    continue
            except psutil.NoSuchProcess:
                pass
            self._logger.debug('Child process disappeared: %s', child)
            self._children.remove(child)

    async def _exterminate_children(self):
        """Terminate all child processes, including those we did not
           spawn directly."""

        self._exterminate_processes(self._children)

    def _exterminate_processes(self, processes):
        """Exterminate the given collection of `psutil.Process`
           processes and their children.

        Method terminates all given processes and their children.
        Algorithm invokes `terminate()` wait a little and invokes
        `kill()`. Does nothing if `processes` is empty.

        Args:
            processes: Collection of `psutil.Process` instances to
                terminate.
        """

        if not processes:
            return

        self._logger.debug(
            'Process extermination sequence initiated for: %s.', [
                p.pid for p in processes]
        )

        # Target extermination weapon to processes and all its children.
        targets = set()
        for p in processes:
            if not p.is_running():
                self._logger.debug(
                    '...process %s has already disappeared!', p.pid
                )
                continue
            targets.update([p])
            try:
                targets.update(p.children(recursive=True))
            except psutil.NoSuchProcess:
                # it is OK if process has already committed suicide
                self._logger.debug(
                    '...process %s has just disappeared!', p.pid
                )

        self._logger.debug('Extermination sequence targets: %s.',
                           [p.pid for p in targets])

        for p in targets:
            if not p.is_running():
                self._logger.debug(
                    '...process %s has already disappeared!', p.pid
                )
                continue

            self._logger.debug('Terminating process %s', p.pid)
            try:
                p.terminate()
                self._logger.debug('...terminate signal sent to %s...', p.pid)
            except psutil.NoSuchProcess:
                # it is OK if process has already committed suicide
                self._logger.debug(
                    '...process %s has just disappeared!', p.pid
                )
            else:
                try:
                    # wait the process to exit gracefully
                    exitcode = p.wait(timeout=self.TERMINATION_TIMEOUT)
                    self._logger.debug(
                        '...process %s finished with exit code %s.',
                        p.pid, exitcode
                    )
                except psutil.TimeoutExpired:
                    # OK, it is still here - kill it
                    self._logger.debug(
                        '...process %s refused to die peacefully, kill it...',
                        p.pid
                    )
                    p.kill()
                    try:
                        exitcode = p.wait(timeout=self.TERMINATION_TIMEOUT)
                        self._logger.debug(
                            '...process %s is finally dead, with exit '
                            'code %s.', p.pid, exitcode
                        )
                    except psutil.TimeoutExpired:
                        # impossible, it is still here, giving up
                        self._logger.error(
                            'Could not kill extremely uncrushable process %s!',
                            p.pid
                        )
