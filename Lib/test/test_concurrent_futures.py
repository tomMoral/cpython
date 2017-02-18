import test.support

# Skip tests if _multiprocessing wasn't built.
test.support.import_module('_multiprocessing')
# Skip tests if sem_open implementation is broken.
test.support.import_module('multiprocessing.synchronize')
# import threading after _multiprocessing to raise a more relevant error
# message: "No module named _multiprocessing". _multiprocessing is not compiled
# without thread support.
test.support.import_module('threading')

from test.support.script_helper import assert_python_ok

import os
import sys
import threading
import time
import unittest
import weakref

from concurrent import futures
from concurrent.futures._base import (
    PENDING, RUNNING, CANCELLED, CANCELLED_AND_NOTIFIED, FINISHED, Future)
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context


def create_future(state=PENDING, exception=None, result=None):
    f = Future()
    f._state = state
    f._exception = exception
    f._result = result
    return f


PENDING_FUTURE = create_future(state=PENDING)
RUNNING_FUTURE = create_future(state=RUNNING)
CANCELLED_FUTURE = create_future(state=CANCELLED)
CANCELLED_AND_NOTIFIED_FUTURE = create_future(state=CANCELLED_AND_NOTIFIED)
EXCEPTION_FUTURE = create_future(state=FINISHED, exception=OSError())
SUCCESSFUL_FUTURE = create_future(state=FINISHED, result=42)


def mul(x, y):
    return x * y


def sleep_and_raise(t):
    time.sleep(t)
    raise Exception('this is an exception')

def sleep_and_print(t, msg):
    time.sleep(t)
    print(msg)
    sys.stdout.flush()


class MyObject(object):
    def my_method(self):
        pass


class ExecutorMixin:
    worker_count = 5

    def setUp(self):
        self.t1 = time.time()
        try:
            self.executor = self.executor_type(max_workers=self.worker_count)
        except NotImplementedError as e:
            self.skipTest(str(e))
        self._prime_executor()

    def tearDown(self):
        self.executor.shutdown(wait=True)
        dt = time.time() - self.t1
        if test.support.verbose:
            print("%.2fs" % dt, end=' ')
        self.assertLess(dt, 60, "synchronization issue: test lasted too long")

    def _prime_executor(self):
        # Make sure that the executor is ready to do work before running the
        # tests. This should reduce the probability of timeouts in the tests.
        futures = [self.executor.submit(time.sleep, 0.1)
                   for _ in range(self.worker_count)]

        for f in futures:
            f.result()


class ThreadPoolMixin(ExecutorMixin):
    executor_type = futures.ThreadPoolExecutor


class ProcessPoolMixin(ExecutorMixin):
    executor_type = futures.ProcessPoolExecutor

    def setUp(self):
        self.t1 = time.time()
        try:
            self.executor = self.executor_type(
                max_workers=self.worker_count,
                ctx=get_context(self.ctx))
        except NotImplementedError as e:
            self.skipTest(str(e))
        self._prime_executor()


class ProcessPoolForkMixin(ProcessPoolMixin):
    ctx = "fork"


class ProcessPoolSpawnMixin(ProcessPoolMixin):
    ctx = "spawn"


class ProcessPoolForkserverMixin(ProcessPoolMixin):
    ctx = "forkserver"


class ExecutorShutdownTest:
    def test_run_after_shutdown(self):
        self.executor.shutdown()
        self.assertRaises(RuntimeError,
                          self.executor.submit,
                          pow, 2, 5)

    def test_interpreter_shutdown(self):
        # Test the atexit hook for shutdown of worker threads and processes
        rc, out, err = assert_python_ok('-c', """if 1:
            from concurrent.futures import {executor_type}
            from time import sleep
            from test.test_concurrent_futures import sleep_and_print
            t = {executor_type}(5)
            t.submit(sleep_and_print, 1.0, "apple")
            """.format(executor_type=self.executor_type.__name__))
        # Errors in atexit hooks don't change the process exit code, check
        # stderr manually.
        self.assertFalse(err)
        self.assertEqual(out.strip(), b"apple")

    def test_hang_issue12364(self):
        fs = [self.executor.submit(time.sleep, 0.1) for _ in range(50)]
        self.executor.shutdown()
        for f in fs:
            f.result()


class ThreadPoolShutdownTest(ThreadPoolMixin, ExecutorShutdownTest, unittest.TestCase):
    def _prime_executor(self):
        pass

    def test_threads_terminate(self):
        self.executor.submit(mul, 21, 2)
        self.executor.submit(mul, 6, 7)
        self.executor.submit(mul, 3, 14)
        self.assertEqual(len(self.executor._threads), 3)
        self.executor.shutdown()
        for t in self.executor._threads:
            t.join()

    def test_context_manager_shutdown(self):
        with futures.ThreadPoolExecutor(max_workers=5) as e:
            executor = e
            self.assertEqual(list(e.map(abs, range(-5, 5))),
                             [5, 4, 3, 2, 1, 0, 1, 2, 3, 4])

        for t in executor._threads:
            t.join()

    def test_del_shutdown(self):
        executor = futures.ThreadPoolExecutor(max_workers=5)
        executor.map(abs, range(-5, 5))
        threads = executor._threads
        del executor

        for t in threads:
            t.join()

    def test_thread_names_assigned(self):
        executor = futures.ThreadPoolExecutor(
            max_workers=5, thread_name_prefix='SpecialPool')
        executor.map(abs, range(-5, 5))
        threads = executor._threads
        del executor

        for t in threads:
            self.assertRegex(t.name, r'^SpecialPool_[0-4]$')
            t.join()

    def test_thread_names_default(self):
        executor = futures.ThreadPoolExecutor(max_workers=5)
        executor.map(abs, range(-5, 5))
        threads = executor._threads
        del executor

        for t in threads:
            # We don't particularly care what the default name is, just that
            # it has a default name implying that it is a ThreadPoolExecutor
            # followed by what looks like a thread number.
            self.assertRegex(t.name, r'^.*ThreadPoolExecutor.*_[0-4]$')
            t.join()


class ProcessPoolShutdownTest(ExecutorShutdownTest):
    def _prime_executor(self):
        pass

    def test_processes_terminate(self):
        self.executor.submit(mul, 21, 2)
        self.executor.submit(mul, 6, 7)
        self.executor.submit(mul, 3, 14)
        self.assertEqual(len(self.executor._processes), 5)
        processes = self.executor._processes
        self.executor.shutdown()

        for p in processes.values():
            p.join()

    def test_context_manager_shutdown(self):
        with futures.ProcessPoolExecutor(max_workers=5) as e:
            processes = e._processes
            self.assertEqual(list(e.map(abs, range(-5, 5))),
                             [5, 4, 3, 2, 1, 0, 1, 2, 3, 4])

        for p in processes.values():
            p.join()

    def test_del_shutdown(self):
        executor = futures.ProcessPoolExecutor(max_workers=5)
        list(executor.map(abs, range(-5, 5)))
        queue_management_thread = executor._queue_management_thread
        processes = executor._processes
        del executor

        queue_management_thread.join()
        for p in processes.values():
            p.join()


class ProcessPoolForkShutdownTest(ProcessPoolForkMixin, unittest.TestCase,
                                  ProcessPoolShutdownTest):
    pass


class ProcessPoolForkserverShutdownTest(ProcessPoolForkserverMixin,
                                        unittest.TestCase,
                                        ProcessPoolShutdownTest):
    pass


class ProcessPoolSpawnShutdownTest(ProcessPoolSpawnMixin, unittest.TestCase,
                                   ProcessPoolShutdownTest):
    pass


class WaitTests:

    def test_first_completed(self):
        future1 = self.executor.submit(mul, 21, 2)
        future2 = self.executor.submit(time.sleep, 1.5)

        done, not_done = futures.wait(
                [CANCELLED_FUTURE, future1, future2],
                 return_when=futures.FIRST_COMPLETED)

        self.assertEqual(set([future1]), done)
        self.assertEqual(set([CANCELLED_FUTURE, future2]), not_done)

    def test_first_completed_some_already_completed(self):
        future1 = self.executor.submit(time.sleep, 1.5)

        finished, pending = futures.wait(
                 [CANCELLED_AND_NOTIFIED_FUTURE, SUCCESSFUL_FUTURE, future1],
                 return_when=futures.FIRST_COMPLETED)

        self.assertEqual(
                set([CANCELLED_AND_NOTIFIED_FUTURE, SUCCESSFUL_FUTURE]),
                finished)
        self.assertEqual(set([future1]), pending)

    def test_first_exception(self):
        future1 = self.executor.submit(mul, 2, 21)
        future2 = self.executor.submit(sleep_and_raise, 1.5)
        future3 = self.executor.submit(time.sleep, 3)

        finished, pending = futures.wait(
                [future1, future2, future3],
                return_when=futures.FIRST_EXCEPTION)

        self.assertEqual(set([future1, future2]), finished)
        self.assertEqual(set([future3]), pending)

    def test_first_exception_some_already_complete(self):
        future1 = self.executor.submit(divmod, 21, 0)
        future2 = self.executor.submit(time.sleep, 1.5)

        finished, pending = futures.wait(
                [SUCCESSFUL_FUTURE,
                 CANCELLED_FUTURE,
                 CANCELLED_AND_NOTIFIED_FUTURE,
                 future1, future2],
                return_when=futures.FIRST_EXCEPTION)

        self.assertEqual(set([SUCCESSFUL_FUTURE,
                              CANCELLED_AND_NOTIFIED_FUTURE,
                              future1]), finished)
        self.assertEqual(set([CANCELLED_FUTURE, future2]), pending)

    def test_first_exception_one_already_failed(self):
        future1 = self.executor.submit(time.sleep, 2)

        finished, pending = futures.wait(
                 [EXCEPTION_FUTURE, future1],
                 return_when=futures.FIRST_EXCEPTION)

        self.assertEqual(set([EXCEPTION_FUTURE]), finished)
        self.assertEqual(set([future1]), pending)

    def test_all_completed(self):
        future1 = self.executor.submit(divmod, 2, 0)
        future2 = self.executor.submit(mul, 2, 21)

        finished, pending = futures.wait(
                [SUCCESSFUL_FUTURE,
                 CANCELLED_AND_NOTIFIED_FUTURE,
                 EXCEPTION_FUTURE,
                 future1,
                 future2],
                return_when=futures.ALL_COMPLETED)

        self.assertEqual(set([SUCCESSFUL_FUTURE,
                              CANCELLED_AND_NOTIFIED_FUTURE,
                              EXCEPTION_FUTURE,
                              future1,
                              future2]), finished)
        self.assertEqual(set(), pending)

    def test_timeout(self):
        future1 = self.executor.submit(mul, 6, 7)
        future2 = self.executor.submit(time.sleep, 6)

        finished, pending = futures.wait(
                [CANCELLED_AND_NOTIFIED_FUTURE,
                 EXCEPTION_FUTURE,
                 SUCCESSFUL_FUTURE,
                 future1, future2],
                timeout=5,
                return_when=futures.ALL_COMPLETED)

        self.assertEqual(set([CANCELLED_AND_NOTIFIED_FUTURE,
                              EXCEPTION_FUTURE,
                              SUCCESSFUL_FUTURE,
                              future1]), finished)
        self.assertEqual(set([future2]), pending)


class ThreadPoolWaitTests(ThreadPoolMixin, WaitTests, unittest.TestCase):

    def test_pending_calls_race(self):
        # Issue #14406: multi-threaded race condition when waiting on all
        # futures.
        event = threading.Event()
        def future_func():
            event.wait()
        oldswitchinterval = sys.getswitchinterval()
        sys.setswitchinterval(1e-6)
        try:
            fs = {self.executor.submit(future_func) for i in range(100)}
            event.set()
            futures.wait(fs, return_when=futures.ALL_COMPLETED)
        finally:
            sys.setswitchinterval(oldswitchinterval)


class ProcessPoolForkWaitTests(ProcessPoolForkMixin, unittest.TestCase,
                               WaitTests):
    pass


class ProcessPoolForkserverWaitTests(ProcessPoolForkserverMixin, WaitTests,
                                     unittest.TestCase):
    pass


class ProcessPoolSpawnWaitTests(ProcessPoolSpawnMixin, unittest.TestCase,
                                WaitTests):
    pass


class AsCompletedTests:
    # TODO(brian@sweetapp.com): Should have a test with a non-zero timeout.
    def test_no_timeout(self):
        future1 = self.executor.submit(mul, 2, 21)
        future2 = self.executor.submit(mul, 7, 6)

        completed = set(futures.as_completed(
                [CANCELLED_AND_NOTIFIED_FUTURE,
                 EXCEPTION_FUTURE,
                 SUCCESSFUL_FUTURE,
                 future1, future2]))
        self.assertEqual(set(
                [CANCELLED_AND_NOTIFIED_FUTURE,
                 EXCEPTION_FUTURE,
                 SUCCESSFUL_FUTURE,
                 future1, future2]),
                completed)

    def test_zero_timeout(self):
        future1 = self.executor.submit(time.sleep, 2)
        completed_futures = set()
        try:
            for future in futures.as_completed(
                    [CANCELLED_AND_NOTIFIED_FUTURE,
                     EXCEPTION_FUTURE,
                     SUCCESSFUL_FUTURE,
                     future1],
                    timeout=0):
                completed_futures.add(future)
        except futures.TimeoutError:
            pass

        self.assertEqual(set([CANCELLED_AND_NOTIFIED_FUTURE,
                              EXCEPTION_FUTURE,
                              SUCCESSFUL_FUTURE]),
                         completed_futures)

    def test_duplicate_futures(self):
        # Issue 20367. Duplicate futures should not raise exceptions or give
        # duplicate responses.
        future1 = self.executor.submit(time.sleep, 2)
        completed = [f for f in futures.as_completed([future1,future1])]
        self.assertEqual(len(completed), 1)


class ThreadPoolAsCompletedTests(ThreadPoolMixin, AsCompletedTests, unittest.TestCase):
    pass



class ProcessPoolForkAsCompletedTests(ProcessPoolForkMixin, AsCompletedTests,
                                      unittest.TestCase):
    pass


class ProcessPoolForkserverAsCompletedTests(ProcessPoolForkserverMixin,
                                            AsCompletedTests,
                                            unittest.TestCase):
    pass


class ProcessPoolSpawnAsCompletedTests(ProcessPoolSpawnMixin, AsCompletedTests,
                                       unittest.TestCase):
    pass


class ExecutorTest:
    # Executor.shutdown() and context manager usage is tested by
    # ExecutorShutdownTest.
    def test_submit(self):
        future = self.executor.submit(pow, 2, 8)
        self.assertEqual(256, future.result())

    def test_submit_keyword(self):
        future = self.executor.submit(mul, 2, y=8)
        self.assertEqual(16, future.result())

    def test_map(self):
        self.assertEqual(
                list(self.executor.map(pow, range(10), range(10))),
                list(map(pow, range(10), range(10))))

    def test_map_exception(self):
        i = self.executor.map(divmod, [1, 1, 1, 1], [2, 3, 0, 5])
        self.assertEqual(i.__next__(), (0, 1))
        self.assertEqual(i.__next__(), (0, 1))
        self.assertRaises(ZeroDivisionError, i.__next__)

    def test_map_timeout(self):
        results = []
        try:
            for i in self.executor.map(time.sleep,
                                       [0, 0, 6],
                                       timeout=5):
                results.append(i)
        except futures.TimeoutError:
            pass
        else:
            self.fail('expected TimeoutError')

        self.assertEqual([None, None], results)

    def test_shutdown_race_issue12456(self):
        # Issue #12456: race condition at shutdown where trying to post a
        # sentinel in the call queue blocks (the queue is full while processes
        # have exited).
        self.executor.map(str, [2] * (self.worker_count + 1))
        self.executor.shutdown()

    @test.support.cpython_only
    def test_no_stale_references(self):
        # Issue #16284: check that the executors don't unnecessarily hang onto
        # references.
        my_object = MyObject()
        my_object_collected = threading.Event()
        my_object_callback = weakref.ref(
            my_object, lambda obj: my_object_collected.set())
        # Deliberately discarding the future.
        self.executor.submit(my_object.my_method)
        del my_object

        collected = my_object_collected.wait(timeout=5.0)
        self.assertTrue(collected,
                        "Stale reference not collected within timeout.")

    def test_max_workers_negative(self):
        for number in (0, -1):
            with self.assertRaisesRegex(ValueError,
                                        "max_workers must be greater "
                                        "than 0"):
                self.executor_type(max_workers=number)


class ThreadPoolExecutorTest(ThreadPoolMixin, ExecutorTest, unittest.TestCase):
    def test_map_submits_without_iteration(self):
        """Tests verifying issue 11777."""
        finished = []
        def record_finished(n):
            finished.append(n)

        self.executor.map(record_finished, range(10))
        self.executor.shutdown(wait=True)
        self.assertCountEqual(finished, range(10))

    def test_default_workers(self):
        executor = self.executor_type()
        self.assertEqual(executor._max_workers,
                         (os.cpu_count() or 1) * 5)


class ProcessPoolExecutorTest(ExecutorTest):
    def test_killed_child(self):
        # When a child process is abruptly terminated, the whole pool gets
        # "broken".
        futures = [self.executor.submit(time.sleep, 3)]
        # Get one of the processes, and terminate (kill) it
        p = next(iter(self.executor._processes.values()))
        p.terminate()
        for fut in futures:
            self.assertRaises(BrokenProcessPool, fut.result)
        # Submitting other jobs fails as well.
        self.assertRaises(BrokenProcessPool, self.executor.submit, pow, 2, 8)

    def test_map_chunksize(self):
        def bad_map():
            list(self.executor.map(pow, range(40), range(40), chunksize=-1))

        ref = list(map(pow, range(40), range(40)))
        self.assertEqual(
            list(self.executor.map(pow, range(40), range(40), chunksize=6)),
            ref)
        self.assertEqual(
            list(self.executor.map(pow, range(40), range(40), chunksize=50)),
            ref)
        self.assertEqual(
            list(self.executor.map(pow, range(40), range(40), chunksize=40)),
            ref)
        self.assertRaises(ValueError, bad_map)

    @classmethod
    def _test_traceback(cls):
        raise RuntimeError(123) # some comment

    def test_traceback(self):
        # We want ensure that the traceback from the child process is
        # contained in the traceback raised in the main process.
        future = self.executor.submit(self._test_traceback)
        with self.assertRaises(Exception) as cm:
            future.result()

        exc = cm.exception
        self.assertIs(type(exc), RuntimeError)
        self.assertEqual(exc.args, (123,))
        cause = exc.__cause__
        self.assertIs(type(cause), futures.process._RemoteTraceback)
        self.assertIn('raise RuntimeError(123) # some comment', cause.tb)

        with test.support.captured_stderr() as f1:
            try:
                raise exc
            except RuntimeError:
                sys.excepthook(*sys.exc_info())
        self.assertIn('raise RuntimeError(123) # some comment',
                      f1.getvalue())


class ProcessPoolForkExecutorTest(ProcessPoolForkMixin,
                                  ProcessPoolExecutorTest,
                                  unittest.TestCase):
    pass


class ProcessPoolForkserverExecutorTest(ProcessPoolForkserverMixin,
                                        ProcessPoolExecutorTest,
                                        unittest.TestCase):
    pass


class ProcessPoolSpawnExecutorTest(ProcessPoolSpawnMixin,
                                   ProcessPoolExecutorTest,
                                   unittest.TestCase):
    pass


def _crash():
    """Induces a segfault"""
    import faulthandler
    faulthandler.disable()
    faulthandler._sigsegv()

def _exit():
    """Induces a sys exit with exitcode 1"""
    sys.exit(1)

def _raise_error(Err):
    """Function that raises an Exception in process"""
    raise Err()

def _return_instance(cls):
    """Function that returns a instance of cls"""
    return cls()

class CrashAtPickle(object):
    """Bad object that triggers a segfault at pickling time."""
    def __reduce__(self):
        _crash()

class CrashAtUnpickle(object):
    """Bad object that triggers a segfault at unpickling time."""
    def __reduce__(self):
        return _crash, ()

class ExitAtPickle(object):
    """Bad object that triggers a segfault at pickling time."""
    def __reduce__(self):
        _exit()

class ExitAtUnpickle(object):
    """Bad object that triggers a process exit at unpickling time."""
    def __reduce__(self):
        return _exit, ()

class ErrorAtPickle(object):
    """Bad object that triggers a segfault at pickling time."""
    def __reduce__(self):
        from pickle import PicklingError
        raise PicklingError("Error in pickle")

class ErrorAtUnpickle(object):
    """Bad object that triggers a process exit at unpickling time."""
    def __reduce__(self):
        from pickle import UnpicklingError
        return _raise_error, (UnpicklingError, )


class TimingWrapper(object):
    """Creates a wrapper for a function which records the time it takes to
    finish
    """
    def __init__(self, func):
        self.func = func
        self.elapsed = None

    def __call__(self, *args, **kwds):
        t = time.time()
        try:
            return self.func(*args, **kwds)
        finally:
            self.elapsed = time.time() - t


class ExecutorDeadlockTest():

    @classmethod
    def _sleep_id(cls, x, delay):
        time.sleep(delay)
        return x

    def test_crash(self):
        # extensive testing for deadlock caused by crash in a pool
        from pickle import PicklingError
        crash_cases = [
            # Check problem occuring while pickling a task in
            # the task_handler thread
            (id, (ExitAtPickle(),), BrokenProcessPool, "exit at task pickle"),
            (id, (ErrorAtPickle(),), BrokenProcessPool, "error at task pickle"),
            # Check problem occuring while unpickling a task on workers
            (id, (ExitAtUnpickle(),), BrokenProcessPool,
             "exit at task unpickle"),
            (id, (ErrorAtUnpickle(),), BrokenProcessPool,
             "error at task unpickle"),
            (id, (CrashAtUnpickle(),), BrokenProcessPool,
             "crash at task unpickle"),
            # Check problem occuring during func execution on workers
            (_crash, (), BrokenProcessPool,
             "crash during func execution on worker"),
            (_exit, (), SystemExit,
             "exit during func execution on worker"),
            (_raise_error, (RuntimeError, ), RuntimeError,
             "error during func execution on worker"),
            # Check problem occuring while pickling a task result
            # on workers
            (_return_instance, (CrashAtPickle,), BrokenProcessPool,
             "crash during result pickle on worker"),
            (_return_instance, (ExitAtPickle,), BrokenProcessPool,
             "exit during result pickle on worker"),
            (_return_instance, (ErrorAtPickle,), BrokenProcessPool,
             "error during result pickle on worker"),
            # Check problem occuring while unpickling a task in
            # the result_handler thread
            (_return_instance, (ExitAtUnpickle,), BrokenProcessPool,
            "exit during result unpickle in result_handler"),
            (_return_instance, (ErrorAtUnpickle,), BrokenProcessPool,
             "error during result unpickle in result_handler")
        ]
        for func, args, error, name in crash_cases:
            with self.subTest(name):
                # skip the test involving pickle errors with manager as it
                # breaks the manager and not the pool in this cases
                # skip the test involving pickle errors with thread as the
                # tasks and results are not pickled in this case
                with test.support.captured_stderr():
                    executor = self.executor_type(max_workers=2,
                                                  ctx=get_context(self.ctx))
                    res = executor.submit(func, *args)
                    with self.assertRaises(error):
                        res.result()
                    executor.shutdown(wait=True)

    @classmethod
    def _test_getpid(cls, a):
        return os.getpid()

    @classmethod
    def _test_kill_worker(cls, pid=None, delay=0.01):
        """Function that send SIGKILL at process pid after delay second"""
        time.sleep(delay)
        if pid is None:
            pid = os.getpid()
        try:
            from signal import SIGKILL
        except ImportError:
            from signal import SIGTERM as SIGKILL
        try:
            os.kill(pid, SIGKILL)
            time.sleep(.01)
        except ProcessLookupError:
            pass

    def test_crash_races(self):

        from itertools import repeat
        for n_proc in [1, 2, 5, 17]:
            with self.subTest(n_proc=n_proc):
                # Test for external crash signal comming from neighbor
                # with various race setup
                executor = self.executor_type(max_workers=2,
                                              ctx=get_context(self.ctx))
                try:
                    raise AttributeError()
                    pids = [p.pid for p in executor._processes]
                    assert len(pids) == n_proc
                except AttributeError:
                    pids = [pid for pid in executor.map(
                       self._test_getpid, [None] * n_proc)]
                assert None not in pids
                res = self.executor.map(
                    self._sleep_id, repeat(True, 2 * n_proc),
                    [.001 * (j // 2) for j in range(2 * n_proc)],
                    chunksize=1)
                assert all(res)
                res = executor.map(self._test_kill_worker, pids[::-1])
                with self.assertRaises(BrokenProcessPool):
                    [v for v in res]
                executor.shutdown(wait=True)

    def test_shutdown_deadlock(self):
        """Test that the pool calling terminate do not cause deadlock
        if a worker failed
        """

        with self.executor_type(max_workers=2,
                                ctx=get_context(self.ctx)) as executor:
            shutdown = TimingWrapper(executor.shutdown)
            executor.submit(self._test_kill_worker, ())
            time.sleep(.01)
            shutdown()
            self.assertLess(shutdown.elapsed, 0.5)

    def test_shutdown_kill(self):
        """ProcessPoolExecutor does not wait job end and shutdowm with
        kill_worker flag set to True.
        """
        from itertools import repeat
        from concurrent.futures.process import ShutdownExecutor
        executor = self.executor_type(max_workers=2,
                                      ctx=get_context(self.ctx))
        res1 = executor.map(self._sleep_id, range(50), repeat(.001))
        res2 = executor.map(self._sleep_id, range(50), repeat(.1))
        assert list(res1) == list(range(50))
        # We should get an error as the executor shutdowned before we fetched
        # the results from the operation.
        shutdown = TimingWrapper(executor.shutdown)
        shutdown(wait=True, kill_workers=True)
        assert shutdown.elapsed < .5
        with self.assertRaises(ShutdownExecutor):
            list(res2)


class ProcessPoolForkExecutorDeadlockTest(ProcessPoolForkMixin,
                                          ExecutorDeadlockTest,
                                          unittest.TestCase):
    pass


class ProcessPoolForkserverExecutorDeadlockTest(ProcessPoolForkserverMixin,
                                                ExecutorDeadlockTest,
                                                unittest.TestCase):
    pass


class ProcessPoolSpawnExecutorDeadlockTest(ProcessPoolSpawnMixin,
                                           ExecutorDeadlockTest,
                                           unittest.TestCase):
    pass


class FutureTests(unittest.TestCase):
    def test_done_callback_with_result(self):
        callback_result = None
        def fn(callback_future):
            nonlocal callback_result
            callback_result = callback_future.result()

        f = Future()
        f.add_done_callback(fn)
        f.set_result(5)
        self.assertEqual(5, callback_result)

    def test_done_callback_with_exception(self):
        callback_exception = None
        def fn(callback_future):
            nonlocal callback_exception
            callback_exception = callback_future.exception()

        f = Future()
        f.add_done_callback(fn)
        f.set_exception(Exception('test'))
        self.assertEqual(('test',), callback_exception.args)

    def test_done_callback_with_cancel(self):
        was_cancelled = None
        def fn(callback_future):
            nonlocal was_cancelled
            was_cancelled = callback_future.cancelled()

        f = Future()
        f.add_done_callback(fn)
        self.assertTrue(f.cancel())
        self.assertTrue(was_cancelled)

    def test_done_callback_raises(self):
        with test.support.captured_stderr() as stderr:
            raising_was_called = False
            fn_was_called = False

            def raising_fn(callback_future):
                nonlocal raising_was_called
                raising_was_called = True
                raise Exception('doh!')

            def fn(callback_future):
                nonlocal fn_was_called
                fn_was_called = True

            f = Future()
            f.add_done_callback(raising_fn)
            f.add_done_callback(fn)
            f.set_result(5)
            self.assertTrue(raising_was_called)
            self.assertTrue(fn_was_called)
            self.assertIn('Exception: doh!', stderr.getvalue())

    def test_done_callback_already_successful(self):
        callback_result = None
        def fn(callback_future):
            nonlocal callback_result
            callback_result = callback_future.result()

        f = Future()
        f.set_result(5)
        f.add_done_callback(fn)
        self.assertEqual(5, callback_result)

    def test_done_callback_already_failed(self):
        callback_exception = None
        def fn(callback_future):
            nonlocal callback_exception
            callback_exception = callback_future.exception()

        f = Future()
        f.set_exception(Exception('test'))
        f.add_done_callback(fn)
        self.assertEqual(('test',), callback_exception.args)

    def test_done_callback_already_cancelled(self):
        was_cancelled = None
        def fn(callback_future):
            nonlocal was_cancelled
            was_cancelled = callback_future.cancelled()

        f = Future()
        self.assertTrue(f.cancel())
        f.add_done_callback(fn)
        self.assertTrue(was_cancelled)

    def test_repr(self):
        self.assertRegex(repr(PENDING_FUTURE),
                         '<Future at 0x[0-9a-f]+ state=pending>')
        self.assertRegex(repr(RUNNING_FUTURE),
                         '<Future at 0x[0-9a-f]+ state=running>')
        self.assertRegex(repr(CANCELLED_FUTURE),
                         '<Future at 0x[0-9a-f]+ state=cancelled>')
        self.assertRegex(repr(CANCELLED_AND_NOTIFIED_FUTURE),
                         '<Future at 0x[0-9a-f]+ state=cancelled>')
        self.assertRegex(
                repr(EXCEPTION_FUTURE),
                '<Future at 0x[0-9a-f]+ state=finished raised OSError>')
        self.assertRegex(
                repr(SUCCESSFUL_FUTURE),
                '<Future at 0x[0-9a-f]+ state=finished returned int>')


    def test_cancel(self):
        f1 = create_future(state=PENDING)
        f2 = create_future(state=RUNNING)
        f3 = create_future(state=CANCELLED)
        f4 = create_future(state=CANCELLED_AND_NOTIFIED)
        f5 = create_future(state=FINISHED, exception=OSError())
        f6 = create_future(state=FINISHED, result=5)

        self.assertTrue(f1.cancel())
        self.assertEqual(f1._state, CANCELLED)

        self.assertFalse(f2.cancel())
        self.assertEqual(f2._state, RUNNING)

        self.assertTrue(f3.cancel())
        self.assertEqual(f3._state, CANCELLED)

        self.assertTrue(f4.cancel())
        self.assertEqual(f4._state, CANCELLED_AND_NOTIFIED)

        self.assertFalse(f5.cancel())
        self.assertEqual(f5._state, FINISHED)

        self.assertFalse(f6.cancel())
        self.assertEqual(f6._state, FINISHED)

    def test_cancelled(self):
        self.assertFalse(PENDING_FUTURE.cancelled())
        self.assertFalse(RUNNING_FUTURE.cancelled())
        self.assertTrue(CANCELLED_FUTURE.cancelled())
        self.assertTrue(CANCELLED_AND_NOTIFIED_FUTURE.cancelled())
        self.assertFalse(EXCEPTION_FUTURE.cancelled())
        self.assertFalse(SUCCESSFUL_FUTURE.cancelled())

    def test_done(self):
        self.assertFalse(PENDING_FUTURE.done())
        self.assertFalse(RUNNING_FUTURE.done())
        self.assertTrue(CANCELLED_FUTURE.done())
        self.assertTrue(CANCELLED_AND_NOTIFIED_FUTURE.done())
        self.assertTrue(EXCEPTION_FUTURE.done())
        self.assertTrue(SUCCESSFUL_FUTURE.done())

    def test_running(self):
        self.assertFalse(PENDING_FUTURE.running())
        self.assertTrue(RUNNING_FUTURE.running())
        self.assertFalse(CANCELLED_FUTURE.running())
        self.assertFalse(CANCELLED_AND_NOTIFIED_FUTURE.running())
        self.assertFalse(EXCEPTION_FUTURE.running())
        self.assertFalse(SUCCESSFUL_FUTURE.running())

    def test_result_with_timeout(self):
        self.assertRaises(futures.TimeoutError,
                          PENDING_FUTURE.result, timeout=0)
        self.assertRaises(futures.TimeoutError,
                          RUNNING_FUTURE.result, timeout=0)
        self.assertRaises(futures.CancelledError,
                          CANCELLED_FUTURE.result, timeout=0)
        self.assertRaises(futures.CancelledError,
                          CANCELLED_AND_NOTIFIED_FUTURE.result, timeout=0)
        self.assertRaises(OSError, EXCEPTION_FUTURE.result, timeout=0)
        self.assertEqual(SUCCESSFUL_FUTURE.result(timeout=0), 42)

    def test_result_with_success(self):
        # TODO(brian@sweetapp.com): This test is timing dependent.
        def notification():
            # Wait until the main thread is waiting for the result.
            time.sleep(1)
            f1.set_result(42)

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        self.assertEqual(f1.result(timeout=5), 42)

    def test_result_with_cancel(self):
        # TODO(brian@sweetapp.com): This test is timing dependent.
        def notification():
            # Wait until the main thread is waiting for the result.
            time.sleep(1)
            f1.cancel()

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        self.assertRaises(futures.CancelledError, f1.result, timeout=5)

    def test_exception_with_timeout(self):
        self.assertRaises(futures.TimeoutError,
                          PENDING_FUTURE.exception, timeout=0)
        self.assertRaises(futures.TimeoutError,
                          RUNNING_FUTURE.exception, timeout=0)
        self.assertRaises(futures.CancelledError,
                          CANCELLED_FUTURE.exception, timeout=0)
        self.assertRaises(futures.CancelledError,
                          CANCELLED_AND_NOTIFIED_FUTURE.exception, timeout=0)
        self.assertTrue(isinstance(EXCEPTION_FUTURE.exception(timeout=0),
                                   OSError))
        self.assertEqual(SUCCESSFUL_FUTURE.exception(timeout=0), None)

    def test_exception_with_success(self):
        def notification():
            # Wait until the main thread is waiting for the exception.
            time.sleep(1)
            with f1._condition:
                f1._state = FINISHED
                f1._exception = OSError()
                f1._condition.notify_all()

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        self.assertTrue(isinstance(f1.exception(timeout=5), OSError))

@test.support.reap_threads
def test_main():
    try:
        test.support.run_unittest(__name__)
    finally:
        test.support.reap_children()

if __name__ == "__main__":
    test_main()
