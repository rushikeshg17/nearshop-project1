"""Migration 2: job_leases and retrain_requests (P1.7, app/jobs.py).

job_leases lets one process at a time run each background job, at any instance count. retrain_requests
queues retraining for the retrain cron job, so training never runs inside the API. Both collections
are new, so this only adds: each is created with its validator and indexes from database/schema.py,
or, where it exists already (a database built by ensure_schema from this release), given them.
Idempotent: a second run changes nothing."""
from __future__ import annotations

import logging

from pymongo.database import Database as MongoDatabase

from database.schema import INDEXES, VALIDATORS

NUMBER = 2
NAME = "job_leases"

COLLECTIONS = ("job_leases", "retrain_requests")

log = logging.getLogger(__name__)


def apply(mongo: MongoDatabase) -> None:
    existing = set(mongo.list_collection_names())
    for name in COLLECTIONS:
        options = {"validator": VALIDATORS[name], "validationLevel": "strict", "validationAction": "error"}
        if name in existing:
            mongo.command("collMod", name, **options)
        else:
            mongo.create_collection(name, **options)
            log.info("created collection %s", name)
        for model in INDEXES.get(name, ()):
            mongo[name].create_indexes([model])  # an index already built is left as it is
