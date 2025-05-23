import logging
import os
import threading
from abc import abstractmethod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from localstack import config
from localstack.services.kinesis.packages import (
    KinesisMockEngine,
    kinesismock_package,
    kinesismock_scala_package,
)
from localstack.utils.common import TMP_THREADS, ShellCommandThread, get_free_tcp_port, mkdir
from localstack.utils.run import FuncThread
from localstack.utils.serving import Server

LOG = logging.getLogger(__name__)


class KinesisMockServer(Server):
    """
    Server abstraction for controlling Kinesis Mock in a separate thread
    """

    def __init__(
        self,
        port: int,
        exe_path: Path,
        latency: str,
        account_id: str,
        host: str = "localhost",
        log_level: str = "INFO",
        data_dir: Optional[str] = None,
    ) -> None:
        self._account_id = account_id
        self._latency = latency
        self._data_dir = data_dir
        self._data_filename = f"{self._account_id}.json"
        self._exe_path = exe_path
        self._log_level = log_level
        super().__init__(port, host)

    def do_start_thread(self) -> FuncThread:
        cmd, env_vars = self._create_shell_command()
        LOG.debug("starting kinesis process %s with env vars %s", cmd, env_vars)
        t = ShellCommandThread(
            cmd,
            strip_color=True,
            env_vars=env_vars,
            log_listener=self._log_listener,
            auto_restart=True,
            name="kinesis-mock",
        )
        TMP_THREADS.append(t)
        t.start()
        return t

    @property
    def _environment_variables(self) -> Dict:
        env_vars = {
            "KINESIS_MOCK_PLAIN_PORT": self.port,
            # Each kinesis-mock instance listens to two ports - secure and insecure.
            # LocalStack uses only one - the insecure one. Block the secure port to avoid conflicts.
            "KINESIS_MOCK_TLS_PORT": get_free_tcp_port(),
            "SHARD_LIMIT": config.KINESIS_SHARD_LIMIT,
            "ON_DEMAND_STREAM_COUNT_LIMIT": config.KINESIS_ON_DEMAND_STREAM_COUNT_LIMIT,
            "AWS_ACCOUNT_ID": self._account_id,
        }

        latency_params = [
            "CREATE_STREAM_DURATION",
            "DELETE_STREAM_DURATION",
            "REGISTER_STREAM_CONSUMER_DURATION",
            "START_STREAM_ENCRYPTION_DURATION",
            "STOP_STREAM_ENCRYPTION_DURATION",
            "DEREGISTER_STREAM_CONSUMER_DURATION",
            "MERGE_SHARDS_DURATION",
            "SPLIT_SHARD_DURATION",
            "UPDATE_SHARD_COUNT_DURATION",
            "UPDATE_STREAM_MODE_DURATION",
        ]
        for param in latency_params:
            env_vars[param] = self._latency

        if self._data_dir and config.KINESIS_PERSISTENCE:
            env_vars["SHOULD_PERSIST_DATA"] = "true"
            env_vars["PERSIST_PATH"] = self._data_dir
            env_vars["PERSIST_FILE_NAME"] = self._data_filename
            env_vars["PERSIST_INTERVAL"] = config.KINESIS_MOCK_PERSIST_INTERVAL

        env_vars["LOG_LEVEL"] = self._log_level

        return env_vars

    @abstractmethod
    def _create_shell_command(self) -> Tuple[List, Dict]:
        """
        Helper method for creating kinesis mock invocation command
        :return: returns a tuple containing the command list and a dictionary with the environment variables
        """
        pass

    def _log_listener(self, line, **_kwargs):
        LOG.info(line.rstrip())


class KinesisMockScalaServer(KinesisMockServer):
    def _create_shell_command(self) -> Tuple[List, Dict]:
        cmd = ["java", "-jar", *self._get_java_vm_options(), str(self._exe_path)]
        return cmd, self._environment_variables

    @property
    def _environment_variables(self) -> Dict:
        default_env_vars = super()._environment_variables
        kinesis_mock_installer = kinesismock_scala_package.get_installer()
        return {
            **default_env_vars,
            **kinesis_mock_installer.get_java_env_vars(),
        }

    def _get_java_vm_options(self) -> list[str]:
        return [
            f"-Xms{config.KINESIS_MOCK_INITIAL_HEAP_SIZE}",
            f"-Xmx{config.KINESIS_MOCK_MAXIMUM_HEAP_SIZE}",
            "-XX:MaxGCPauseMillis=500",
            "-XX:+ExitOnOutOfMemoryError",
        ]


class KinesisMockNodeServer(KinesisMockServer):
    @property
    def _environment_variables(self) -> Dict:
        node_env_vars = {
            # Use the `server.json` packaged next to the main.js
            "KINESIS_MOCK_CERT_PATH": str((self._exe_path.parent / "server.json").absolute()),
        }

        default_env_vars = super()._environment_variables
        return {**node_env_vars, **default_env_vars}

    def _create_shell_command(self) -> Tuple[List, Dict]:
        cmd = ["node", self._exe_path]
        return cmd, self._environment_variables


class KinesisServerManager:
    default_startup_timeout = 60

    def __init__(self):
        self._lock = threading.RLock()
        self._servers: dict[str, KinesisMockServer] = {}

    def get_server_for_account(self, account_id: str) -> KinesisMockServer:
        if account_id in self._servers:
            return self._servers[account_id]

        with self._lock:
            if account_id in self._servers:
                return self._servers[account_id]

            LOG.info("Creating kinesis backend for account %s", account_id)
            self._servers[account_id] = self._create_kinesis_mock_server(account_id)
            self._servers[account_id].start()
            if not self._servers[account_id].wait_is_up(timeout=self.default_startup_timeout):
                raise TimeoutError("gave up waiting for kinesis backend to start up")
            return self._servers[account_id]

    def shutdown_all(self):
        with self._lock:
            while self._servers:
                account_id, server = self._servers.popitem()
                LOG.info("Shutting down kinesis backend for account %s", account_id)
                server.shutdown()

    def _create_kinesis_mock_server(self, account_id: str) -> KinesisMockServer:
        """
        Creates a new Kinesis Mock server instance. Installs Kinesis Mock on the host first if necessary.
        Introspects on the host config to determine server configuration:
        config.dirs.data -> if set, the server runs with persistence using the path to store data
        config.LS_LOG -> configure kinesis mock log level (defaults to INFO)
        config.KINESIS_LATENCY -> configure stream latency (in milliseconds)
        """
        port = get_free_tcp_port()

        # kinesis-mock stores state in json files <account_id>.json, so we can dump everything into `kinesis/`
        persist_path = os.path.join(config.dirs.data, "kinesis")
        mkdir(persist_path)
        if config.KINESIS_MOCK_LOG_LEVEL:
            log_level = config.KINESIS_MOCK_LOG_LEVEL.upper()
        elif config.LS_LOG:
            ls_log_level = config.LS_LOG.upper()
            if ls_log_level == "WARNING":
                log_level = "WARN"
            elif ls_log_level == "TRACE-INTERNAL":
                log_level = "TRACE"
            elif ls_log_level not in ("ERROR", "WARN", "INFO", "DEBUG", "TRACE"):
                # to protect from cases where the log level will be rejected from kinesis-mock
                log_level = "INFO"
            else:
                log_level = ls_log_level
        else:
            log_level = "INFO"
        latency = config.KINESIS_LATENCY + "ms"

        # Install the Scala Kinesis Mock build if specified in KINESIS_MOCK_PROVIDER_ENGINE
        if KinesisMockEngine(config.KINESIS_MOCK_PROVIDER_ENGINE) == KinesisMockEngine.SCALA:
            kinesismock_scala_package.install()
            kinesis_mock_path = Path(
                kinesismock_scala_package.get_installer().get_executable_path()
            )

            return KinesisMockScalaServer(
                port=port,
                exe_path=kinesis_mock_path,
                log_level=log_level,
                latency=latency,
                data_dir=persist_path,
                account_id=account_id,
            )

        # Otherwise, install the NodeJS version (default)
        kinesismock_package.install()
        kinesis_mock_path = Path(kinesismock_package.get_installer().get_executable_path())

        return KinesisMockNodeServer(
            port=port,
            exe_path=kinesis_mock_path,
            log_level=log_level,
            latency=latency,
            data_dir=persist_path,
            account_id=account_id,
        )
