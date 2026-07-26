"""Publishing bounded context — domain layer (α8.6 Creator Workflow).

- α8.6a — the **account-connection** aggregate (:class:`SocialAccount`).
- α8.6b — the **publish-runtime** aggregate (:class:`PublishJob`) + the platform-agnostic
  :class:`ContentPackage` it carries. Destination adapters live in infrastructure
  (credential-blind, PUB-5); the real YouTube adapter is α8.6c.
"""

from app.domain.publishing.content_package import (
    ContentPackage,
    Visibility,
    build_content_package,
)
from app.domain.publishing.publish_job import PublishJob, PublishJobClaim
from app.domain.publishing.publish_status import PublishStatus
from app.domain.publishing.social_account import AccountStatus, SocialAccount

__all__ = [
    "AccountStatus",
    "SocialAccount",
    "PublishStatus",
    "PublishJob",
    "PublishJobClaim",
    "ContentPackage",
    "Visibility",
    "build_content_package",
]
