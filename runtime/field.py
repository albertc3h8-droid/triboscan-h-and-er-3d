"""Reserved field-rebuild entry point (exclusion of measured points/groups).

The current product always builds the field from every measured point.  The
future workflow - exclude points/groups, then rebuild the field and the
display-spacing statistics - must not change the cache schema or the app
boundary, so this module carries the signature from
``docs/02_app_architecture_interfaces.md`` §9.  Nothing here is wired to the
UI yet.

Contract for the future implementation (do not weaken it):

* exclusion is by stable IDs only: ``point_id`` = ``P:<file>#<physical line>``
  and ``group_id`` = ``G:<x_nm>,<y_nm>``;
* unknown IDs must fail loudly (``InputError``/``ValueError``), never be
  silently ignored;
* the cache keeps the last full field plus
  ``active_point_ids_sha256`` (``null`` means "all points"), so a future
  rebuild can be cached without a new format version;
* a rebuild is a compute-stage operation (background worker), not a render
  operation.
"""
from __future__ import annotations

from typing import Sequence


def recompute_with_exclusions(
    dataset,
    excluded_group_ids: Sequence[str] = (),
    excluded_point_ids: Sequence[str] = (),
    params=None,
):
    """Rebuild ``Dataset + FieldGrid`` after excluding stable IDs.

    Reserved interface: the first version does not implement exclusion UI or
    the rebuild itself.  It raises immediately so a caller can never mistake
    a silently-unchanged field for a successful rebuild.
    """
    raise NotImplementedError(
        'exclusion-based field rebuild is reserved for a future version')
