# -*- coding: utf-8 -
#
# This file is part of gunicorn released under the MIT license.
# See the NOTICE for more information.

import sys

# supported gunicorn workers.
SUPPORTED_WORKERS={
        "sync": "gunicorn.workers.sync.SyncWorker",
        "eventlet": "gunicorn.workers.geventlet.EventletWorker",
        "gevent": "gunicorn.workers.ggeventpause.MozSvcGeventWorker",
        "gevent_wsgi": "gunicorn.workers.ggevent.GeventPyWSGIWorker",
        "gevent_pywsgi": "gunicorn.workers.ggevent.GeventPyWSGIWorker",
        "tornado": "gunicorn.workers.gtornado.TornadoWorker"}


if sys.version_info >= (3, 3):
    # gaiohttp worker can be used with Python 3.3+ only.
    SUPPORTED_WORKERS["gaiohttp"] = "gunicorn.workers.gaiohttp.AiohttpWorker"
