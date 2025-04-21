"""
Class for executing callables on specified pool.
"""

from concurrent.futures import Executor, Future, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional

from ch_backup import logging


@dataclass
class Job:
    """
    Job submitted to ExecPool.

    Callback is executed after job completion.
    """

    id_: str
    callback: Optional[Callable]


class ExecPool:
    """
    Submit tasks on provided executor.

    Encapsulate collecting of futures and waiting all submitted tasks.
    """

    def __init__(self, executor: Executor) -> None:
        self._future_to_job: Dict[Future, Job] = {}
        self._pool = executor
        # It is necessary to start all processes while there are no running threads
        # Used to freeze and backup tables at the same time
        self._start_processes()

    def shutdown(self, graceful: bool = True) -> None:
        """
        Wait workers for complete jobs and shutdown workers
        """
        self._pool.shutdown(wait=graceful)

    def submit(
        self,
        job_id: str,
        func: Callable,
        callback: Optional[Callable],
        *args: Any,
        **kwargs: Any
    ) -> None:
        """
        Schedule job for execution
        """
        if job_id in [job.id_ for job in self._future_to_job.values()]:
            raise RuntimeError("Duplicate")

        future = self._pool.submit(func, *args, **kwargs)
        self._future_to_job[future] = Job(job_id, callback)

    @staticmethod
    def _start():
        return

    def _start_processes(self):
        future = self._pool.submit(ExecPool._start)
        future.result()

    def _as_completed(self, keep_going: bool = False, timeout: Optional[float] = None) -> Iterable:
        for future in as_completed(self._future_to_job, timeout):
            job = self._future_to_job[future]
            logging.debug("Future {} completed", job.id_)

            try:
                result = future.result()
                yield (job, result)
            except Exception:
                if keep_going:
                    logging.warning(
                        'Job "{}" generated an exception, skipping due to keep_going flag',
                        job.id_,
                        exc_info=True,
                    )
                    continue
                logging.error(
                    'Job "{}" generated an exception:', job.id_, exc_info=True
                )
                raise

    def as_completed(self, keep_going: bool = False, timeout: Optional[float] = None) -> Iterable:
        """
        Return result from futures as they are completed.

        Args:
            keep_going - skip exceptions raised by futures instead of propagating it.
        """
        for job, result in self._as_completed(keep_going, timeout):
            if job.callback:
                job.callback()

            yield result

        self._future_to_job = {}

    def wait_all(self, keep_going: bool = False, timeout: Optional[float] = None) -> None:
        """
        Wait workers for complete jobs.

        Args:
            keep_going - skip exceptions raised by futures instead of propagating it.
        """
        for job, _ in self._as_completed(keep_going, timeout):
            if job.callback:
                job.callback()

        self._future_to_job = {}

    def __del__(self) -> None:
        """
        Shutdown pool explicitly to prevent the program from hanging in case of ungraceful termination.
        """
        try:
            self.shutdown(graceful=True)
        except Exception:  # nosec B110
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.shutdown(graceful=True)
        except Exception:  # nosec B110
            pass
        return False
