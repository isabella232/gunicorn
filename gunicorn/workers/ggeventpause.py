# ***** BEGIN LICENSE BLOCK *****
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
# ***** END LICENSE BLOCK *****
"""
Custom gevent-based worker class for gunicorn.

This module provides a custom GeventWorker subclass for gunicorn, with some
extra operational niceties.
"""

import greenlet
import os
import signal
import sys
import thread
import threading
import time
import traceback

import gevent.hub
from lyft import logging

from gunicorn.workers.ggevent import GeventWorker

stats = None

try:
    import statsd

    stats = statsd.StatsClient(prefix='gunicorn.worker')
except ImportError:
    pass

logger = logging.getLogger(__name__)

# Take references to un-monkey-patched versions of stuff we need.
# Monkey-patching will have already been done by the time we come to
# use these functions at runtime.
_real_sleep = time.sleep
_real_start_new_thread = thread.start_new_thread
_real_event = threading.Event
_real_get_ident = thread.get_ident

# The maximum amount of time that the eventloop can be blocked
# without causing an error to be logged.
MAX_BLOCKING_TIME = float(os.environ.get("GEVENT_MAX_BLOCKING_TIME", 10))


class MozSvcGeventWorker(GeventWorker):
    """Custom gunicorn worker with extra operational niceties.

    This is a custom gunicorn worker class, based on the standard gevent worker
    but with some extra operational- and debugging-related features:

        * a background thread that monitors execution by checking for:

            * blocking of the gevent event-loop, with tracebacks
              logged if blocking code is found.

        * a timeout enforced on each individual request, rather than on
          inactivity of the worker as a whole.

    To detect eventloop blocking, the worker installs a greenlet trace
    function that increments a counter on each context switch.  A background
    (os-level) thread monitors this counter and prints a traceback if it has
    not changed within a configurable number of seconds.
    """

    def init_process(self):
        # Check if we need a background thread to monitor memory use.
        needs_monitoring_thread = False
        # Set up a greenlet tracing hook to monitor for event-loop blockage,
        # but only if monitoring is both possible and required.
        if hasattr(greenlet, "settrace") and MAX_BLOCKING_TIME > 0:
            # Grab a reference to the gevent hub.
            # It is needed in a background thread, but is only visible from
            # the main thread, so we need to store an explicit reference to it.
            self._active_hub = gevent.hub.get_hub()
            # Set up a trace function to record each greenlet switch.
            self._active_greenlet = None
            self._greenlet_switch_counter = 0
            # The settrace function returns the previous tracer
            # We'll save it so we can call it after we are done tracing
            self.previous_trace = greenlet.settrace(self._greenlet_switch_tracer)
            self._main_thread_id = _real_get_ident()
            needs_monitoring_thread = True

        # Create a real thread to monitor out execution.
        # Since this will be a long-running daemon thread, it's OK to
        # fire-and-forget using the low-level start_new_thread function.
        if needs_monitoring_thread:
            self._stop_event = _real_event()
            self._stopped_monitoring = _real_event()
            _real_start_new_thread(self._process_monitoring_thread, ())

        # Continue to superclass initialization logic.
        # Note that this runs the main loop and never returns.
        super(MozSvcGeventWorker, self).init_process()

    def init_signals(self):
        # Leave all signals defined by the superclass in place.
        super(MozSvcGeventWorker, self).init_signals()
        if hasattr(signal, "siginterrupt"):
            signal.siginterrupt(signal.SIGUSR2, False)

    def handle_request(self, *args):
        # Apply the configured 'timeout' value to each individual request.
        # Note that self.timeout is set to half the configured timeout by
        # the arbiter, so we use the value directly from the config.
        with gevent.Timeout(self.cfg.timeout):
            return super(MozSvcGeventWorker, self).handle_request(*args)

    def stop_monitoring(self, wait_timeout=10):
        # Stops the thread monitoring for pause events
        # If a timeout is specified, will wait up to that amount of time
        # Returns True if the monitoring was stopped before the timeout
        self._stop_event.set()
        stopped = self._stopped_monitoring.wait(wait_timeout)
        return stopped

    def _greenlet_switch_tracer(self, event, args):
        """Callback method executed on every greenlet switch.

        The worker arranges for this method to be called on every greenlet
        switch.  It keeps track of which greenlet is currently active and
        increments a counter to track how many switches have been performed.
        """
        # Increment the counter to indicate that a switch took place.
        # This will periodically be reset to zero by the monitoring thread,
        # so we don't need to worry about it growing without bound.

        # For an explanation about why we look at these particular event types,
        # see https://greenlet.readthedocs.io/en/latest/ [Tracing Support]
        if event == 'switch' or event == 'throw':
            origin, target = args
            self._active_greenlet = target
            self._greenlet_switch_counter += 1
            self._last_switch_time = time.time()

        # If there was a previously registered tracer,
        # we can call it now
        if self.previous_trace:
            self.previous_trace(event, args)

    def _process_monitoring_thread(self):
        """Method run in background thread that monitors our execution.

        This method is an endless loop that gets executed in a background
        thread.  It periodically wakes up and checks:

            * whether the active greenlet has switched since last checked
        """
        # Find the minimum interval between checks.
        sleep_interval = MAX_BLOCKING_TIME

        logger.info("Starting pause detector for greenlets taking longer than %s seconds", MAX_BLOCKING_TIME)

        # Run the checks in an infinite sleeping loop.
        try:
            while not self._stop_event.isSet():
                _real_sleep(sleep_interval)
                self._check_greenlet_blocking()
        except Exception:
            # Swallow any exceptions raised during interpreter shutdown.
            # Daemonic Thread objects have this same behaviour.
            if sys is not None:
                raise
        finally:
            if logger:
                logger.info("Pause detector is exiting")
            self._stopped_monitoring.set()

    def _check_greenlet_blocking(self):
        if not MAX_BLOCKING_TIME:
            return
        # If there have been no greenlet switches since we last checked,
        # grab the stack trace and log an error.  The active greenlet's frame
        # is not available from the greenlet object itself, we have to look
        # up the current frame of the main thread for the traceback.
        if self._greenlet_switch_counter == 0:
            active_greenlet = self._active_greenlet
            # The hub gets a free pass, since it blocks waiting for IO.
            if active_greenlet not in (None, self._active_hub):
                frame = sys._current_frames()[self._main_thread_id]
                stack = traceback.format_stack(frame)
                time_running = time.time() - self._last_switch_time
                err_message = "Greenlet appears to be blocked\n"
                err_message += "It has been running over %.2f seconds (max time allowed is %s seconds)\n" % (
                    time_running, MAX_BLOCKING_TIME)
                err_log = [err_message] + stack
                logger.error("".join(err_log))
                if stats is not None:
                    stats.incr("paused")

        # Reset the count to zero.
        # This might race with it being incremented in the main thread,
        # but not often enough to cause a false positive.
        self._greenlet_switch_counter = 0
