import os
import itertools
import re
import pdb
import json
from collections import defaultdict

import debug
import debug as Dbg
import config
import sqs

import config as Cfg
import debug as Dbg
from notify import record_call as R

from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.core import patch_all
patch_all()

import cslog
log = cslog.logger(__name__)

class RDS:
    def __init__(self, context, o_state, o_cloudwatch):
        self.context     = context
        self.o_state     = o_state
        self.cloudwatch  = o_cloudwatch

    def get_prerequisites(self):
        rds_client     = self.context["rds.client"]
        tagging_client = self.context["resourcegroupstaggingapi.client"]

        self.databases = {"db": [], "cluster": []}
        for db_type in list(self.databases.keys()):
            paginator = tagging_client.get_paginator('get_resources')
            tag_mappings = itertools.chain.from_iterable(
                page['ResourceTagMappingList']
                    for page in paginator.paginate(
                        ResourceTypeFilters=["rds:%s" % db_type],
                        TagFilters=[
                            {
                                'Key': 'clonesquad:group-name',
                                'Values': [ self.context["GroupName"] ]
                            }]
                        )
                )
            self.databases["%s.tags" % db_type] = list(tag_mappings)
            if len(self.databases["%s.tags" % db_type]) == 0:
                continue
            if db_type == "cluster":
                func           = rds_client.describe_db_clusters
                filter_key     = "db-cluster-id"
                response_index = "DBClusters"
            if db_type == "db":
                func           = rds_client.describe_db_instances
                filter_key     = "db-instance-id"
                response_index = "DBInstances"
            self.databases[db_type].extend(func(
                Filters=[
                    {
                        'Name': filter_key,
                         'Values': [ t["ResourceARN"] for t in self.databases["%s.tags" % db_type] ]
                    }]
                )[response_index])

        #log.debug(Dbg.pprint(self.databases))

        Cfg.register({
                 "rds.state.default_ttl" : "hours=2",
                 "rds.metrics.time_resolution": "60",
            })

        self.state_table = self.o_state.get_state_table()
        self.state_table.register_aggregates([
            {
                "Prefix": "rds.",
                "Compress": True,
                "DefaultTTL": Cfg.get_duration_secs("rds.state.default_ttl"),
                "Exclude" : []
            }
            ])

        metric_time_resolution = Cfg.get_int("rds.metrics.time_resolution")
        if metric_time_resolution < 60: metric_time_resolution = 1 # Switch to highest resolution
        self.cloudwatch.register_metric([
                { "MetricName": "StaticFleet.RDS.Size",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                { "MetricName": "StaticFleet.RDS.AvailableDBs",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                { "MetricName": "StaticFleet.RDS.StoppingDBs",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                { "MetricName": "StaticFleet.RDS.StartingDBs",
                  "Unit": "Count",
                  "StorageResolution": metric_time_resolution },
                ])

        # We need to register dynamically static subfleet configuration keys to avoid a 'key unknown' warning 
        #   when the user is going to set it
        static_subfleet_names = self.get_rds_subfleet_names()
        for static_fleet in static_subfleet_names:
            key = "staticfleet.%s.state" % static_fleet
            if not Cfg.is_builtin_key_exist(key):
                Cfg.register({
                    key : ""
                    })
        log.log(log.NOTICE, "Detected following static subfleet names across RDS resources: %s" % static_subfleet_names)

    @xray_recorder.capture()
    def manage_subfleet_rds(self):
        """Manage start/stop actions for static subfleet RDS instances
        """
        states = defaultdict(int)
        arns   = self.get_subfleet_arns()
        for arn in arns:
            subfleet_name  = self.get_static_subfleet_name_for_db(arn)
            forbidden_chars = "[ .]"
            if re.match(forbidden_chars, subfleet_name):
                log.warning("Subfleet name '%s' contains invalid characters (%s)!! Ignore this DB..." % (arn, forbidden_chars))
                continue
            expected_state = Cfg.get("staticfleet.%s.state" % subfleet_name, none_on_failure=True)
            if expected_state is None:
                log.log(log.NOTICE, "Encountered a static fleet RDS database (%s) without state directive. Please set 'staticfleet.%s.state' configuration key..." % 
                        (arn, subfleet_name))
                continue
            db_expected_state = expected_state if expected_state != "running" else "available"

            current_state  = self.get_db_status(arn)
            log.debug("Manage static fleet DB '%s': subfleet_name=%s, current_state=%s, expected_state=%s" % 
                    (arn, subfleet_name, current_state, expected_state))
            if expected_state != "" and db_expected_state != current_state:
                log.info("RDS database '%s' is transitionning from '%s' to '%s' state..." % 
                        (self.get_db_id(arn), current_state, db_expected_state))
            states[current_state] += 1

            allowed_expected_states = ["running", "stopped", "undefined", ""]
            if expected_state not in allowed_expected_states:
                log.warning("Expected state '%s' for static subfleet '%s' is not valid : (not in %s!)" % (expected_state, subfleet_name, allowed_expected_states))
                continue

            if expected_state == "running" and self.get_db_status(arn) == "stopped":
                self.start_db(arn)
            if expected_state == "stopped" and self.get_db_status(arn) == "available":
                self.stop_db(arn)

        cw = self.cloudwatch
        if len(arns):
            cw.set_metric("StaticFleet.RDS.Size", len(arns))
            cw.set_metric("StaticFleet.RDS.AvailableDBs", states["available"])
            cw.set_metric("StaticFleet.RDS.StoppingDBs", states["stopping"])
            cw.set_metric("StaticFleet.RDS.StartingDBs", states["starting"])
        else:
            cw.set_metric("StaticFleet.RDS.Size", None)
            cw.set_metric("StaticFleet.RDS.AvailableDBs", None)
            cw.set_metric("StaticFleet.RDS.StoppingDBs", None)
            cw.set_metric("StaticFleet.RDS.StartingDBs", None)

    def stop_db(self, arn):
        client  = self.context["rds.client"]
        db      = self.get_rds_db(arn)
        db_type = db["_Meta"]["dbType"]
        log.info("Stopping RDS DB '%s' (type:%s)" % (arn, db_type))
        try:
            if db_type == "cluster":
                response = R(lambda args, kwargs, r: "DBCluster" in r,
                        client.stop_db_cluster, DBClusterIdentifier=db["DBClusterIdentifier"])
            if db_type == "db":
                response = R(lambda args, kwargs, r: "DBInstance" in r,
                        client.stop_db_instance, DBInstanceIdentifier=db["DBInstanceIdentifier"])
        except Exception as e:
            log.warning("Got exception while stopping DB '%s'! : %s" % (arn, e))
        log.debug(Dbg.pprint(response))

    def start_db(self, arn):
        client  = self.context["rds.client"]
        db      = self.get_rds_db(arn)
        db_type = db["_Meta"]["dbType"]
        log.info("Starting RDS DB '%s' (type:%s)" % (arn, db_type))
        try:
            if db_type == "cluster":
                response = R(lambda args, kwargs, r: "DBCluster" in r,
                    client.start_db_cluster, DBClusterIdentifier=db["DBClusterIdentifier"])
            if db_type == "db":
                response = R(lambda args, kwargs, r: "DBInstance" in r,
                    client.start_db_instance, DBInstanceIdentifier=db["DBInstanceIdentifier"])
        except Exception as e:
            log.warning("Got exception while starting DB '%s'! : %s" % (arn, e))
        log.debug(response)

    def get_db_status(self, arn):
        db = self.get_rds_db(arn)
        if db is None:
            return None
        return db["DBInstanceStatus"] if "DBInstanceStatus" in db else db["Status"]

    def get_db_id(self, arn):
        db = self.get_rds_db(arn)
        if db is None:
            return None
        return db["DBInstanceIdentifier"] if "DBInstanceIdentifier" in db else db["DBClusterIdentifier"]

    def get_rds_tags(self, db_arn):
        for db_type in ["db.tags", "cluster.tags"]:
            db_tag = next(filter(lambda r: r["ResourceARN"] == db_arn, self.databases[db_type]), None)
            if db_tag is None:
                continue
            tags = {}
            for t in db_tag["Tags"]:
                tags[t["Key"]] = t["Value"]
            return tags
        return None

    def get_static_subfleet_name_for_db(self, arn):
        tags = self.get_rds_tags(arn)
        if "clonesquad:static-subfleet-name" in tags:
            return tags["clonesquad:static-subfleet-name"]
        return None

    def get_rds_subfleet_names(self):
        names = []
        for db_type in ["db.tags", "cluster.tags"]:
            for db in self.databases[db_type]:
                db_arn = db["ResourceARN"]
                if self.get_rds_db(db_arn) is None:
                    continue
                tags   = self.get_rds_tags(db_arn)
                if "clonesquad:static-subfleet-name" in tags:
                    if "clonesquad:excluded" in tags and tags["clonesquad:excluded"] in ["True", "true"]:
                        continue
                    static_subfleet_name = tags["clonesquad:static-subfleet-name"]
                    if static_subfleet_name not in names: names.append(static_subfleet_name)
        return names

    def get_subfleet_arns(self):
        arns = []
        for db_type in ["db.tags", "cluster.tags"]:
            for db in self.databases[db_type]:
                arn = db["ResourceARN"]
                if self.get_rds_db(arn) is None:
                    continue
                if arn not in arns: arns.append(arn)
        return arns

    def get_rds_db(self, arn):
        for db_type in ["db", "cluster"]:
            for db in self.databases[db_type]:
                db_arn = db["DBClusterArn"] if "DBClusterArn" in db else db["DBInstanceArn"] if "DBInstanceArn" in db else None
                if db_arn != arn:
                    continue
                db["_Meta"] = {
                    "dbType" : db_type
                }
                return db
        return None