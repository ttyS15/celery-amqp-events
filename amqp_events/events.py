from typing import Callable

from celery import Celery
from celery.canvas import Signature
from celery.result import AsyncResult
from kombu import Exchange, Queue

from amqp_events import tasks, defaults


class EventsCelery(Celery):

    def __init__(self, main, *args, **kwargs):
        super().__init__(main, *args, **kwargs)
        self.on_after_finalize.connect(self._generate_task_queues)
        self.on_after_finalize.connect(self._register_retry_queues)

    def event(self, name: str) -> Callable[[Callable], "Event"]:
        def inner(func):
            return Event(self, name, func)

        return inner

    def handler(self, name: str, bind: bool = False
                ) -> Callable[[Callable], Callable]:
        return self.task(name=name, base=tasks.EventHandler, bind=bind)

    def _generate_task_queues(self, **_):
        queues = self.conf.task_queues
        if queues:
            return
        exchange = Exchange(
            name=self.conf.task_default_exchange,
            type=self.conf.task_default_exchange_type)
        for name, task in self._tasks.items():
            if task.__module__.startswith('celery.'):
                continue
            queue = Queue(
                name=f'{self.main}.{task.name}',
                exchange=exchange,
                routing_key=task.name)
            queues.append(queue)

    def _register_retry_queues(self, **_):
        channel = self.broker_connection().default_channel
        for queue in self.conf.task_queues:
            retry_queue = Queue(
                name=f'{queue.name}.retry',
                routing_key=f'{queue.routing_key}.retry',
                exchange=queue.exchange,
                queue_arguments={
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": queue.name
                }
            )

            retry_queue.declare(channel=channel)
            retry_queue.maybe_bind(channel=channel)

            archived_queue = Queue(
                name=f'{queue.name}.archived',
                routing_key=f'{queue.routing_key}.archived',
                exchange=queue.exchange,
                queue_arguments={
                    "x-message-ttl": defaults.AMQP_EVENTS_ARCHIVED_MESSAGE_TTL,
                    "x-max-length": defaults.AMQP_EVENTS_ARCHIVED_QUEUE_LENGTH,
                    "x-queue-mode": "lazy"
                })

            archived_queue.declare(channel=channel)
            archived_queue.maybe_bind(channel=channel)


class Event:
    def __init__(self, app: EventsCelery, name: str, func: Callable) -> None:
        self.app = app
        self.name = name
        self.func = func

    def __call__(self, *args, **kwargs) -> str:
        self.func(*args, **kwargs)
        s = self.make_signature(args, kwargs)
        result: AsyncResult = s.apply_async()
        return result.id

    def handler(self, func: Callable) -> Callable:
        return self.app.handler(self.name)(func)

    def make_signature(self, args, kwargs):
        return Signature(
            args=args,
            kwargs=kwargs,
            task=self.name,
            app=self.app,
            task_type=self.app.Task,
            routing_key=self.name)
