"""core/api/publisher.py: how the API tells the queue that a job exists.

Navigation:
  JobPublisher   protocol: publish(job_id)
  PublishFailed  a publish that did not succeed (carries no payload)

Only the job id travels: state lives in Postgres, never in the message. The Kafka
implementation (KafkaJobPublisher) is not built yet; it needs the local Kafka from
Component 7. Tests use the recording fake in tests/fakes.py.
"""
from typing import Protocol


class PublishFailed(Exception):
    """The broker did not acknowledge the message. The job stays `queued` (see Q1)."""


class JobPublisher(Protocol):
    def publish(self, job_id: str) -> None:
        """Publish `job_id`; raise PublishFailed if it was not acknowledged."""
