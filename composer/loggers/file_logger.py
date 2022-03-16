# Copyright 2021 MosaicML. All Rights Reserved.

"""Logs to a file or to the terminal."""

from __future__ import annotations

import os
import queue
import sys
from typing import Any, Callable, Dict, Optional, TextIO

import yaml

from composer.core.state import State
from composer.loggers.logger import Logger, LoggerDataDict, LogLevel, format_log_data_value
from composer.loggers.logger_destination import LoggerDestination
from composer.utils import dist

__all__ = ["FileLogger"]


class FileLogger(LoggerDestination):
    """Log data to a file.

    Example usage:
        .. testcode::

            from composer.loggers import FileLogger, LogLevel
            from composer.trainer import Trainer
            logger = FileLogger(
                filename_format="{run_name}/logs-rank{rank}.txt",
                buffer_size=1,
                log_level=LogLevel.BATCH,
                log_interval=2,
                flush_interval=50
            )
            trainer = Trainer(
                model=model,
                train_dataloader=train_dataloader,
                eval_dataloader=eval_dataloader,
                max_duration="1ep",
                optimizers=[optimizer],
                loggers=[logger]
            )

        .. testcleanup::

            try:
                os.remove(logger.filename)
            except FileNotFoundError as e:
                pass

    Example output::

        [FIT][step=2]: { "logged_metric": "logged_value", }
        [EPOCH][step=2]: { "logged_metric": "logged_value", }
        [BATCH][step=2]: { "logged_metric": "logged_value", }
        [EPOCH][step=3]: { "logged_metric": "logged_value", }


    Args:
        filename_format (str, optional): Format string for the filename.

            The following format variables are available:

            +------------------------+-------------------------------------------------------+
            | Variable               | Description                                           |
            +========================+=======================================================+
            | ``{run_name}``         | The name of the training run. See                     |
            |                        | :attr:`~composer.core.logging.Logger.run_name`.       |
            +------------------------+-------------------------------------------------------+
            | ``{rank}``             | The global rank, as returned by                       |
            |                        | :func:`~composer.utils.dist.get_global_rank`.         |
            +------------------------+-------------------------------------------------------+
            | ``{local_rank}``       | The local rank of the process, as returned by         |
            |                        | :func:`~composer.utils.dist.get_local_rank`.          |
            +------------------------+-------------------------------------------------------+
            | ``{world_size}``       | The world size, as returned by                        |
            |                        | :func:`~composer.utils.dist.get_world_size`.          |
            +------------------------+-------------------------------------------------------+
            | ``{local_world_size}`` | The local world size, as returned by                  |
            |                        | :func:`~composer.utils.dist.get_local_world_size`.    |
            +------------------------+-------------------------------------------------------+
            | ``{node_rank}``        | The node rank, as returned by                         |
            |                        | :func:`~composer.utils.dist.get_node_rank`.           |
            +------------------------+-------------------------------------------------------+

            .. note::

                When training with multiple devices (i.e. GPUs), ensure that ``'{rank}'`` appears in the format.
                Otherwise, multiple processes may attempt to write to the same file.

            Consider the following example when using default value of '{run_name}/logs-rank{rank}.txt':

            >>> file_logger = FileLogger(filename_format='{run_name}/logs-rank{rank}.txt')
            >>> trainer = Trainer(logger_destinations=[file_logger], run_name='my-awesome-run')
            >>> trainer.file_logger.filename
            'my-awesome-run/logs-rank0.txt'

            Default: `'{run_name}/logs-rank{rank}.txt'`

        artifact_name_format (str, optional): Format string for the logfile's artifact name.
        
            The logfile will be periodically logged (according to the ``flush_interval``) as a file artifact.
            The artifact name will be determined by this format string.

            .. seealso:: :meth:`~composer.core.logging.Logger.log_file_artifact` for file artifact logging.

            The same format variables for ``filename_format`` are available. Setting this parameter to ``None``
            (the default) will use the same format string as ``filename_format``. It is sometimes helpful to deviate
            from this default. For example, when ``filename_format`` contains an absolute path, it is recommended to
            set this parameter explicitely, so the absolute path does not appear in any artifact stores.

            Leading slashes (``'/'``) will be stripped.

            Default: ``None`` (which uses the same format string as ``filename_format``)
        capture_stdout (bool, optional): Whether to include the ``stdout``in ``filename``. (default: ``True``)
        capture_stderr (bool, optional): Whether to include the ``stderr``in ``filename``. (default: ``True``)
        buffer_size (int, optional): Buffer size. See :py:func:`open`.
            Default: ``1`` for line buffering.
        log_level (LogLevel, optional):
            :class:`~.logger.LogLevel` (i.e. unit of resolution) at
            which to record. Default: :attr:`~.LogLevel.EPOCH`.
        log_interval (int, optional):
            Frequency to print logs. If ``log_level`` is :attr:`~.LogLevel.EPOCH`,
            logs will only be recorded every n epochs. If ``log_level`` is
            :attr:`~.LogLevel.BATCH`, logs will be printed every n batches.  Otherwise, if
            ``log_level`` is :attr:`~.LogLevel.FIT`, this parameter is ignored, as calls
            at the :attr:`~.LogLevel.FIT` log level are always recorded. Default: ``1``.
        flush_interval (int, optional): How frequently to flush the log to the file,
            relative to the ``log_level``. For example, if the ``log_level`` is
            :attr:`~.LogLevel.EPOCH`, then the logfile will be flushed every n epochs.  If
            the ``log_level`` is :attr:`~.LogLevel.BATCH`, then the logfile will be
            flushed every n batches. Default: ``100``.
    """

    def __init__(
        self,
        filename_format: str = "{run_name}/logs-rank{rank}.txt",
        artifact_name_format: Optional[str] = None,
        *,
        capture_stdout: bool = True,
        capture_stderr: bool = True,
        buffer_size: int = 1,
        log_level: LogLevel = LogLevel.EPOCH,
        log_interval: int = 1,
        flush_interval: int = 100,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self.filename_format = filename_format
        if artifact_name_format is None:
            artifact_name_format = filename_format.replace(os.path.sep, '/')
        self.artifact_name_format = artifact_name_format
        self.buffer_size = buffer_size
        self.log_level = log_level
        self.log_interval = log_interval
        self.flush_interval = flush_interval
        self.is_batch_interval = False
        self.is_epoch_interval = False
        self.file: Optional[TextIO] = None
        self.config = config
        self._queue: queue.Queue[str] = queue.Queue()
        self._original_stdout_write = sys.stdout.write
        self._original_stderr_write = sys.stderr.write
        self._run_name = None

        if capture_stdout:
            sys.stdout.write = self._get_new_writer("[stdout]: ", self._original_stdout_write)

        if capture_stderr:
            sys.stderr.write = self._get_new_writer("[stderr]: ", self._original_stderr_write)

    def _get_new_writer(self, prefix: str, original_writer: Callable[[str], int]):
        """Returns a writer that intercepts calls to the ``original_writer``."""

        def new_write(s: str) -> int:
            self.write(prefix, s)
            return original_writer(s)

        return new_write

    @property
    def filename(self) -> str:
        """The filename for the logfile."""
        if self._run_name is None:
            raise RuntimeError("The run name is not set. The engine should have been set on Event.INIT")
        name = self.filename_format.format(
            rank=dist.get_global_rank(),
            local_rank=dist.get_local_rank(),
            world_size=dist.get_world_size(),
            local_world_size=dist.get_local_world_size(),
            node_rank=dist.get_node_rank(),
            run_name=self._run_name,
        )

        return name

    @property
    def artifact_name(self) -> str:
        """The artifact name for the logfile."""
        if self._run_name is None:
            raise RuntimeError("The run name is not set. The engine should have been set on Event.INIT")
        name = self.filename_format.format(
            rank=dist.get_global_rank(),
            local_rank=dist.get_local_rank(),
            world_size=dist.get_world_size(),
            local_world_size=dist.get_local_world_size(),
            node_rank=dist.get_node_rank(),
            run_name=self._run_name,
        )

        name.lstrip("/")

        return name

    def batch_start(self, state: State, logger: Logger) -> None:
        self.is_batch_interval = (int(state.timer.batch) + 1) % self.log_interval == 0

    def epoch_start(self, state: State, logger: Logger) -> None:
        self.is_epoch_interval = (int(state.timer.epoch) + 1) % self.log_interval == 0
        # Flush any log calls that occurred during INIT or FIT_START
        self._flush_file(logger)

    def _will_log(self, log_level: LogLevel) -> bool:
        if log_level == LogLevel.FIT:
            return True  # fit is always logged
        if log_level == LogLevel.EPOCH:
            if self.log_level < LogLevel.EPOCH:
                return False
            if self.log_level > LogLevel.EPOCH:
                return True
            return self.is_epoch_interval
        if log_level == LogLevel.BATCH:
            if self.log_level < LogLevel.BATCH:
                return False
            if self.log_level > LogLevel.BATCH:
                return True
            return self.is_batch_interval
        raise ValueError(f"Unknown log level: {log_level}")

    def log_data(self, state: State, log_level: LogLevel, data: LoggerDataDict):
        if not self._will_log(log_level):
            return
        data_str = format_log_data_value(data)
        self.write(
            f'[{log_level.name}][batch={int(state.timer.batch)}]: ',
            data_str + "\n",
        )

    def init(self, state: State, logger: Logger) -> None:
        del state  # unused
        self._run_name = logger.run_name
        if self.file is not None:
            raise RuntimeError("The file logger is already initialized")
        self.file = open(self.filename, "x+", buffering=self.buffer_size)
        self._flush_queue()
        if self.config is not None:
            data = ("-" * 30) + "\n" + yaml.safe_dump(self.config) + "\n" + ("-" * 30) + "\n"
            self.write('[config]: ', data)

    def batch_end(self, state: State, logger: Logger) -> None:
        assert self.file is not None
        if self.log_level == LogLevel.BATCH and int(state.timer.batch) % self.flush_interval == 0:
            self._flush_file(logger)

    def eval_start(self, state: State, logger: Logger) -> None:
        # Flush any log calls that occurred during INIT when using the trainer in eval-only mode
        self._flush_file(logger)

    def epoch_end(self, state: State, logger: Logger) -> None:
        if self.log_level > LogLevel.EPOCH or self.log_level == LogLevel.EPOCH and int(
                state.timer.epoch) % self.flush_interval == 0:
            self._flush_file(logger)

    def write(self, prefix: str, s: str):
        """Write to the logfile.

        .. note::

            If the ``write`` occurs before the :attr:`~composer.core.event.Event.INIT` event,
            the write will be enqueued, as the file is not yet open.

        Args:
            prefix (str): A prefix for each line in the logfile.
            s (str): The string to write. Each line will be prefixed with ``prefix``.
        """
        formatted_lines = []
        for line in s.splitlines(True):
            if line == os.linesep:
                # If it's an empty line, don't print the prefix
                formatted_lines.append(line)
            else:
                formatted_lines.append(f"{prefix}{line}")
        formatted_s = ''.join(formatted_lines)
        if self.file is None:
            self._queue.put_nowait(formatted_s)
        else:
            # Flush the queue, so all prints will be in order
            self._flush_queue()
            # Then, write to the file
            print(formatted_s, file=self.file, flush=False, end='')

    def _flush_queue(self):
        while True:
            try:
                s = self._queue.get_nowait()
            except queue.Empty:
                break
            print(s, file=self.file, flush=False, end='')

    def _flush_file(self, logger: Logger) -> None:
        assert self.file is not None

        self._flush_queue()

        self.file.flush()
        os.fsync(self.file.fileno())
        logger.file_artifact(LogLevel.FIT, self.artifact_name, self.file.name, overwrite=True)

    def close(self, state: State, logger: Logger) -> None:
        del state  # unused
        if self.file is not None:
            sys.stdout.write = self._original_stdout_write
            sys.stderr.write = self._original_stderr_write
            self._flush_file(logger)
            self.file.close()
            self.file = None
            self._run_name = None
