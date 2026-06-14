#    Copyright (c) 2025 Rich Bell <bellrichm@gmail.com>
#
#    See the file LICENSE.txt for your full rights.
#

"""
Generate aggregated values for other extensions
"""

import configobj
import copy
import logging
import os
import pickle
import traceback
import weecfg
import weeutil
import weewx

from datetime import datetime
from weeutil.weeutil import to_bool, TimeSpan
from weewx.engine import StdService

VERSION = "1.0.0"

def resolve_obs_group(observation, agg_type):
    """
    Return the unit group for an aggregated observation, mirroring
    the logic weewx.xtypes.get_aggregate uses internally.

    - Most agg types (sum, avg, min, max, …) inherit the observation's group.
    - Time-based agg types (mintime, maxtime, …) override to group_time.
    """
    # agg_group maps agg types that produce a different group than the obs itself
    # e.g. {"mintime": "group_time", "maxtime": "group_time", "count": "group_count", ...}
    override_group = weewx.units.agg_group.get(agg_type)
    if override_group is not None:
        return override_group

    # Fall back to the observation's own group
    return weewx.units.obs_group_dict.get(observation)

# log = logging.getLogger(__name__)
def setup_logging(logging_level, config_dict):
    """ Setup logging for running in standalone mode."""
    if logging_level:
        weewx.debug = logging_level

    weeutil.logger.setup("AggregatedValues", config_dict)

class Logger:
    """ Manage the logging """
    def __init__(self):
        self.log = logging.getLogger(__name__)

    def logdbg(self, msg):
        """ log debug messages """
        self.log.debug(msg)

    def loginf(self, msg):
        """ log informational messages """
        self.log.info(msg)

    def logerr(self, msg):
        """ log error messages """
        self.log.error(msg)

class StrorageClass():

    def __init__(self):

        self.dt = datetime.now()
        self.yesterday = None
        self.month = None
        self.last_month = None
        self.year = None
        self.last_year = None

class TimeSpanProvider:
    """ Manage the timespans. """
    def __init__(self, logger, week_start, since_hour, first_timestamp):
        self.logger = logger
        self.week_start = week_start
        self.period_timespans = {
            "hour": self.hour,
            "day": self.day,
            "yesterday": self.yesterday,
            "week": self.week,
            "month": self.month,
            "last_month": self.last_month,
            "year": self.year,
            "last_year": self.last_year,
            "last3hours": self.last3hours,
            "last24hours": self.last24hours,
            "last7days": self.last7days,
            "last31days": self.last31days,
            "last366days": self.last366days,
            "since": self.since,
        }

        if 0 < since_hour > 23:
            raise ValueError("since_hour must be between 0 and 23 or not set")

        self.since_seconds = since_hour * 3600

        if first_timestamp is None or first_timestamp <= 0:
            raise ValueError("first_timestamp needs to be > 0")

        self.first_timestamp = first_timestamp

    def get_timespan(self, agg_dict, timestamp):
        """ Get a timespan for the interval and timstamp. """

        interval = agg_dict["period"]

        if interval == "since":
            return self.since(agg_dict, timestamp)

        return self.period_timespans[interval](timestamp)

    def hour(self, timestamp):
        """ Get a timespan for the hour. """
        return weeutil.weeutil.archiveHoursAgoSpan(timestamp)

    def day(self, timestamp):
        """ Get a timespan for the day. """
        return weeutil.weeutil.archiveDaySpan(timestamp)

    def yesterday(self, timestamp):
        """ Get a timespan for yesterday. """
        return weeutil.weeutil.archiveDaySpan(timestamp, 1)

    def week(self, timestamp):
        """ Get a timespan for the running week. """
        return weeutil.weeutil.archiveWeekSpan(timestamp, startOfWeek=self.week_start)

    def month(self, timestamp):
        """ Get a timespan for the running month. """
        return weeutil.weeutil.archiveMonthSpan(timestamp)

    def last_month(self, timestamp):
        """ Get a timespan for the previous month. """
        return weeutil.weeutil.archiveMonthSpan(timestamp, 1)

    def year(self, timestamp):
        """ Get a timespan for the running year. """
        return weeutil.weeutil.archiveYearSpan(timestamp)

    def last_year(self, timestamp):
        """ Get a timespan for the previous year. """
        return weeutil.weeutil.archiveYearSpan(timestamp, 1)

    def last3hours(self, timestamp):
        """ Get a timespan for the past 3 hours. """
        return TimeSpan(timestamp - 10800, timestamp)

    def last24hours(self, timestamp):
        """ Get a timespan for the last 24 hours. """
        return TimeSpan(timestamp - 86400, timestamp)

    def last7days(self, timestamp):
        """ Get a timespan for the last 7 days. """
        return self._last_n_days(7, timestamp)

    def last31days(self, timestamp):
        """ Get a timespan for the last 31 days. """
        return self._last_n_days(31, timestamp)

    def last366days(self, timestamp):
        """ Get a timespan for the last 366 days. """
        return self._last_n_days(366, timestamp)

    def _last_n_days(self, days, timestamp):
        """ Get a TimeSpan for the last N days """
        return TimeSpan(time.mktime((datetime.date.fromtimestamp(timestamp) - datetime.timedelta(days=days)).timetuple()), timestamp)

    def shift_timespan(self, current_timespan):
        """ Shift the start/stop time by since_seconds """
        return TimeSpan(current_timespan.start + self.since_seconds, current_timespan.stop + self.since_seconds)

    def check_timespan(self, timespan, timestamp, time_to_subtract):
        """ Make sure the timespan includes the timestamp """

        if timespan.start <= timestamp <= timespan.stop:
            return timespan

        timespan = TimeSpan(timespan.start - time_to_subtract, timespan.stop - time_to_subtract)

        if timespan.stop < timestamp:
            return self.check_timespan(timespan, timestamp, -time_to_subtract)

        if timespan.start <= timestamp <= timespan.stop:
            return timespan

        return self.check_timespan(timespan, timestamp, time_to_subtract)

    def since(self, agg_dict, timestamp):
        """ Get a TimeSpan for offset of since_hours from midnight """

        time_to_subtract = 86400
        if to_bool(agg_dict.get("last_month", False)):
            time_to_subtract = 2419200
        elif to_bool(agg_dict.get("last_year", False)):
            time_to_subtract = 31536000

        if to_bool(agg_dict.get("yesterday", False)):
            timespan = self.yesterday(timestamp)
        elif to_bool(agg_dict.get("week", False)):
            timespan = self.week(timestamp)
        elif to_bool(agg_dict.get("month", False)):
            timespan = self.month(timestamp)
        elif to_bool(agg_dict.get("last_month", False)):
            timespan = self.last_month(timestamp)
        elif to_bool(agg_dict.get("year", False)):
            timespan = self.year(timestamp)
        elif to_bool(agg_dict.get("last_year", False)):
            timespan = self.last_year(timestamp)
        else:
            timespan = self.day(timestamp)

        if self.since_seconds == 0:
            return timespan

        if to_bool(agg_dict.get("yesterday", False)) or \
            to_bool(agg_dict.get("last_month", False)) or \
            to_bool(agg_dict.get("last_year", False)):
            timestamp -= time_to_subtract

        timespan = self.shift_timespan(timespan)

        return self.check_timespan(timespan, timestamp, time_to_subtract)

class AggregatedValuesService(StdService):
    """ A service to publish WeeWX loop and/or archive data to MQTT. """
    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)

        self.version = "1.0.0"

        self.storage = None

        self.logger = Logger()

        self.process_config_dict(config_dict)

        self.logger.loginf(f"AggregatedValues version: {self.version}")

        service_dict = config_dict.get("AggregatedValues", {})

        #self.logger.logdbg(f"service_dict is {service_dict}")

        self.enable = to_bool(service_dict.get("enable", True))
        if not self.enable:
            self.logger.loginf("Not enabled, exiting.")
            return

        data_binding = service_dict.get("data_binding", "wx_binding")

        self.db_manager = self.engine.db_binder.get_manager(data_binding=data_binding)

        self.timespan_provider = TimeSpanProvider(self.logger, engine.stn_info.week_start, \
                                                  int(service_dict.get("since_hour", 0)), \
                                                  self.db_manager.firstGoodStamp())

        self.fields = self.configure_fields(service_dict)

        self.pickle_filename = "/etc/weewx/AggregatedValues.pkl"

        self.load_pickle()

        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

    def load_pickle(self):
        self.logger.logdbg(f"Attempting to load cached data from {self.pickle_filename}")

        if os.path.exists(self.pickle_filename):
            try:
                with open(self.pickle_filename, "rb") as f:

                    ret = pickle.load(f)

                    if isinstance(ret, StrorageClass):
                        self.storage = ret

            except Exception as e:
                pass

        if self.storage is None:
            self.storage = StrorageClass()
            self.save_pickle()

    def save_pickle(self):
        self.logger.logdbg(f"Attempting to save cached data to {self.pickle_filename}")

        try:
            with open(self.pickle_filename, "wb") as f:
                pickle.dump(self.storage, f)
        except Exception as e:
            self.logger.logerr(f" Error!, e: {str(e)}")

    def process_config_dict(self, config_dict):

        try:
            root_dict = weeutil.startup.extract_roots(config_dict)
            if root_dict is not None:
                ext_dir = root_dict.get("EXT_DIR", None)
                if ext_dir is not None:
                    ext_cache_dir = os.path.join(ext_dir, "AggregatedValues")
                    _, installer = weecfg.get_extension_installer(ext_cache_dir)
                    self.version = installer.get("version", "1.0.0")

        except Exception as e:
            self.logger.logerr(f"Error! Unable to get extension version, e: {str(e)}")

    def configure_fields(self, service_dict):
        """ Configure the fields """

        fields = {}
        for field in service_dict.sections:

            if not field:
                # skip blank fields
                continue

            field_dict = service_dict.get(field, {})
            ignore = to_bool(field_dict.get("ignore", False))
            if ignore:
                continue

            observation = field_dict.get("observation")
            if observation is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have an observation set, skipping...")
                continue

            aggregation = field_dict.get("aggregation")
            if aggregation is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have an aggregation set, skipping...")
                continue

            if field_dict.get("period") is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have a period set, skipping...")
                continue

            resolved_group = resolve_obs_group(observation, aggregation)
            if resolved_group is None:
                self.logger.logerr(f"Error! Field '{field}' has observation '{observation}' and aggregation '{aggregation}' but the group type can't be resolved, skipping...")
                continue

            #self.logger.logdbg(f"{observation} with {aggregation} resolved to {resolved_group}")

            for timeperiod in [None, "yesterday", "month", "last_month", "year", "last_year"]:
                output_name = field
                if timeperiod is not None:
                    period = field_dict.get("period")
                    if period == "day":
                        output_name = timeperiod + "_" + field
                        #self.logger.logdbg(f"output_name: {output_name}")
                    elif period == "since":
                        output_name = timeperiod + "_" + field
                        #self.logger.logdbg(f"output_name: {output_name}")
                    else:
                        continue

                if weewx.units.obs_group_dict.get(output_name) is None:
                    #self.logger.logdbg(f"Set '{output_name}' to be in {resolved_group}")
                    weewx.units.obs_group_dict[output_name] = resolved_group

            fields[field] = field_dict

        return fields

    def generate_records(self, dateTime, timeperiod=None):

        new_record = {}

        for field in self.fields:

            try:
                field_dict = copy.deepcopy(self.fields[field])

                period = field_dict["period"]
                agg = field_dict["aggregation"]

                output_name = field
                if timeperiod is not None:
                    if period == "day":
                        field_dict["period"] = timeperiod
                        output_name = timeperiod + "_" + field
                        #self.logger.logdbg(f"output_name: {output_name}")
                    elif period == "since":
                        field_dict[timeperiod] = True
                        output_name = timeperiod + "_" + field
                        #self.logger.logdbg(f"output_name: {output_name}")
                    else:
                        continue

                time_span = self.timespan_provider.get_timespan(field_dict, dateTime)

                vt = weewx.xtypes.get_aggregate(field_dict["observation"], time_span, agg, self.db_manager)

                converted_vt = weewx.units.convertStd(vt, weewx.US)

                #self.logger.logdbg(f"converted_vt: {converted_vt}")

                new_record[output_name] = converted_vt.value

            except Exception as exception:
                tb = traceback.format_exc()
                self.logger.logerr(f"Aggregation failed: {tb}")

        return new_record


    def new_archive_record(self, event):
        record = event.record

        dt = datetime.fromtimestamp(record["dateTime"])

        if self.storage.yesterday is None or dt.date() != self.storage.dt.date():
            self.storage.yesterday = self.generate_records(record["dateTime"], "yesterday")
            self.storage.month = self.generate_records(record["dateTime"], "month")
            self.storage.year = self.generate_records(record["dateTime"], "year")
            self.storage.dt = dt

        if self.storage.last_month is None or dt.month != self.storage.dt.month:
            self.storage.last_month = self.generate_records(record["dateTime"], "last_month")
            self.storage.dt = dt

        if self.storage.last_year is None or dt.year != self.storage.dt.year:
            self.storage.last_year = self.generate_records(record["dateTime"], "last_year")
            self.storage.dt = dt

        new_record = self.generate_records(record["dateTime"])

        for records in [new_record, self.storage.yesterday, self.storage.month, self.storage.last_month, self.storage.year, self.storage.last_year]:
            keys = records.keys()
            for key in keys:
                record[key] = records[key]

        self.save_pickle()

    def shutDown(self):
        """Run when an engine shutdown is requested."""
        self.save_pickle()
        self.logger.loginf("Shutdown initiatead")
