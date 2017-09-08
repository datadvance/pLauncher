pLauncher
=========

pLauncher spawns group of services and watch over them.

Features:

* Declarative configuration.

* Allow services to have dynamic configuration: service may start with
  dynamically selected parameter value. It is developed in order to
  provide ability to start service on randomly selected port.

* Allow services to have shared context. For example when one service
  starts on randomly selected port, configuration of another service may
  refer to it.

* Pure Python project works under Linux, Windows and Mac.

Example
-------

.. code-block:: python

    worker_cfg = {
        'name': 'Worker',

        'params': {
            'PORT': lambda: random.randint(49152, 65535),
            'HOST': '127.0.0.1',
            'PATH': '/home/users',
        },

        'endpoints': [('{Worker_HOST}', '{Worker_PORT}')],
        'attempts': 5,

        'env': {'PATH': '{Worker_PATH}'}
        'cmd': ['./worker', '--host={Worker_HOST}', '--port={Worker_PORT}'],
    }

    server_cfg = {
        'name': 'Server',

        'endpoints': [('127.0.0.1', '8080')],
        'attempts': 3,

        'cmd': ['./server',
                '--worker-host={Worker_HOST}',
                '--worker-port={Worker_PORT}'],
    }

    launcher = Launcher([worker_cfg, server_cfg])
    try:
        await launcher.run()
        await launcher.wait()
    finally:
        await launcher.shutdown()
