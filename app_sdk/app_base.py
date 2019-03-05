import asyncio
import json
import logging
import os
import sys
import time
from contextlib import asynccontextmanager

import aioredis

from .message_types import ActionStatus, message_dumps
from .workflow_types import workflow_loads

# get app environment vars
REDIS_URI = os.getenv("REDIS_URI", "redis://localhost")
ACTION_RESULT_CH = os.getenv("ACTION_RESULT_CH", "action-results")
ACTIONS_IN_PROCESS = os.getenv("ACTIONS_IN_PROCESS", "actions-in-process")


class AppBase:
    def __init__(self, redis=None, logger=None):
        # Creates redis keys of format "{AppName}-{Version}-{Priority}"
        self.action_queue_keys = tuple(f"{self.__class__.__name__}-{self.__version__}-{i}" for i in range(5, 0, -1))
        print(self.action_queue_keys)
        self.redis: aioredis.Redis = redis
        self.logger = logger if logger is not None else logging.getLogger("AppBaseLoggerShouldBeOverridden")

    @asynccontextmanager
    async def connect_to_redis_pool(self, redis_uri=REDIS_URI) -> aioredis.Redis:
        # Redis client bound to pool of connections (auto-reconnecting).
        self.redis = await aioredis.create_redis_pool(redis_uri)
        try:
            yield self.redis
        finally:
            # gracefully close pool
            self.redis.close()
            await self.redis.wait_closed()
            self.logger.info("Redis connection pool closed.")

    async def get_actions(self):
        """ Continuously monitors the action queues and asynchronously executes actions """
        self.logger.debug("Waiting for actions...")
        while True:
            # Currently brpoplpush does not accept a list of keys like brpop does. To achieve the same functionality
            # we iteratively call the non-blocking rpoplpush on each of the app queues in priority order and grab the
            # first result we can
            action = None
            i = 0
            start = time.time()
            while action is None:
                src_key = self.action_queue_keys[i % len(self.action_queue_keys)]
                action = await self.redis.rpoplpush(src_key, ACTIONS_IN_PROCESS)
                i += 1
                await asyncio.sleep(0)

                if time.time() - start > 30: # We've timed out with no work. Guess we'll die now...
                    sys.exit(1)

            action = workflow_loads(action)
            asyncio.create_task(self.execute_action(action))

    async def execute_action(self, action):
        """ Execute an action and ship its result """
        self.logger.debug(f"Attempting execution of: {action.name}-{action.workflow_execution_id}")
        if hasattr(self, action.action_name):
            start_action_msg = ActionStatus.executing_from_action(action)
            await self.redis.lpush(action.workflow_execution_id, message_dumps(start_action_msg))
            try:
                func = getattr(self, action.action_name, None)
                if callable(func):
                    if len(action.parameters) < 1:
                        result = func()
                    else:
                        result = func(**{p.name: p.value for p in action.parameters})
                    action_result = ActionStatus.success_from_action(action, result)
                    self.logger.debug(f"Executed {action.name}-{action.id_} with result: {result}")

                else:
                    self.logger.error(f"App {self.__class__.__name__}.{action.action_name} is not callable")
                    action_result = ActionStatus.failure_from_action(action, error="Action not callable")

            except Exception as e:
                action_result = ActionStatus.failure_from_action(action=action, error=repr(e))
                self.logger.exception(f"Failed to execute {action.name}-{action.id_}")

            await self.redis.lpush(action.workflow_execution_id, message_dumps(action_result))

        else:
            self.logger.error(f"App {self.__class__.__name__} has no method {action.action_name}")
            action_result = ActionStatus.from_action(action, error="Action does not exist")
            await self.redis.lpush(action.workflow_execution_id, message_dumps(action_result))
