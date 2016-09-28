import greenlet
import os

import gevent
import mock
import pytest

from gunicorn.config import Config
from gunicorn.workers import ggeventpause

MAX_BLOCK_TIME_SEC = 0.1


def func_sleep(timeout=None):
    ggeventpause._real_sleep(timeout)


def func_sleep_over_max():
    func_sleep(MAX_BLOCK_TIME_SEC * 3)


def func_do_nothing():
    pass


def _create_ggeventpause_worker(mocker):
    app = mock.MagicMock()
    log = mock.MagicMock()
    worker = ggeventpause.MozSvcGeventWorker('age',
                                             os.getppid(),
                                             [],
                                             app,
                                             'timeout',
                                             Config(),
                                             log)
    # We don't really want to run the gunicorn loop so mock it out
    # Otherwise, init_process will start it and not return
    run_mock = mocker.patch.object(worker, 'run')
    worker.init_process()

    return worker


@pytest.fixture
def logger_mock(mocker):
    return mocker.patch('gunicorn.workers.ggeventpause.logger')


@pytest.yield_fixture
@pytest.fixture
def ggeventpause_worker(mocker):
    ggeventpause.MAX_BLOCKING_TIME = MAX_BLOCK_TIME_SEC
    ggeventpause.WARMUP_TIME = 0
    worker = _create_ggeventpause_worker(mocker)

    yield worker

    # Stop the worker from monitoring once we are done
    # The assert is there to guarantee that the monitor was stopped
    assert worker.stop_monitoring()


@pytest.fixture
def greenlet_tracer_mock(mocker):
    tracer = mock.MagicMock()
    greenlet.settrace(tracer)
    return tracer


class MatchContainsString:
    def __init__(self, substring):
        self.substring = substring

    def __eq__(self, strWithSubstring):
        return strWithSubstring.find(self.substring) >= 0


def test_logs_greenlets_over_max_time(mocker, logger_mock, ggeventpause_worker):
    gevent.joinall([
        gevent.spawn(func_sleep_over_max),
        gevent.spawn(func_do_nothing),
        gevent.spawn(lambda: func_sleep(MAX_BLOCK_TIME_SEC / 3))
    ])

    assert logger_mock.error.called
    logger_mock.error.assert_called_with(MatchContainsString("func_sleep_over_max"))


def test_calls_previous_tracers(mocker, logger_mock, greenlet_tracer_mock, ggeventpause_worker):
    gevent.spawn(func_sleep_over_max).join()

    assert greenlet_tracer_mock.called


def test_stop_stops_monitoring(mocker, logger_mock, ggeventpause_worker):
    ggeventpause_worker.stop_monitoring()

    gevent.spawn(func_sleep_over_max).join()

    assert not logger_mock.error.called


def test_does_not_log_while_warming_up(mocker, logger_mock):
    ggeventpause.MAX_BLOCKING_TIME = MAX_BLOCK_TIME_SEC
    ggeventpause.WARMUP_TIME = 10
    worker = _create_ggeventpause_worker(mocker)

    gevent.joinall([
        gevent.spawn(func_sleep_over_max)
    ])

    assert not logger_mock.error.called
