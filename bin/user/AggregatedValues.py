#    Copyright (c) 2025 Rich Bell <bellrichm@gmail.com>
#
#    See the file LICENSE.txt for your full rights.
#

"""
Generate aggregated values for other extensions
"""

import configobj
import logging
import os
import weecfg
import weeutil
import weewx

from weeutil.weeutil import to_bool, TimeSpan
from weewx.engine import StdService

VERSION = "1.0.0"

# log = logging.getLogger(__name__)
def setup_logging(logging_level, config_dict):
    """ Setup logging for running in standalone mode."""
    if logging_level:
        weewx.debug = logging_level

    weeutil.logger.setup('AggregatedValues', config_dict)

class Logger:
    ''' Manage the logging '''
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

class TimeSpanProvider:
    ''' Manage the timespans. '''
    def __init__(self, week_start, since_hour=0):
        self.week_start = week_start
        self.period_timespans = {
            'hour': self.hour,
            'day': self.day,
            'yesterday': self.yesterday,
            'week': self.week,
            'month': self.month,
            'year': self.year,
            'last3hours': self.last3hours,
            'last24hours': self.last24hours,
            'last7days': self.last7days,
            'last31days': self.last31days,
            'last366days': self.last366days,
            'since': self.since,
        }

        if 0 < since_hour > 23:
            raise ValueError("since_hour must be between 0 and 23 or not set")

        self.since_seconds = since_hour * 3600

    def get_timespan(self, agg_dict, timestamp):
        ''' Get a timespan for the interval and timstamp. '''

        interval = agg_dict["period"]

        if interval == "since":
            return self.since(agg_dict, timestamp)

        return self.period_timespans[interval](timestamp)

    def hour(self, timestamp):
        ''' Get a timespan for the hour. '''
        return weeutil.weeutil.archiveHoursAgoSpan(timestamp)

    def last3hours(self, timestamp):
        ''' Get a timespan for the past 3 hours. '''
        return weeutil.weeutil.archiveHoursAgoSpan(timestamp, 3)

    def day(self, timestamp):
        ''' Get a timespan for the day. '''
        return weeutil.weeutil.archiveDaySpan(timestamp)

    def yesterday(self, timestamp):
        ''' Get a timespan for yesterday. '''
        return weeutil.weeutil.archiveDaySpan(timestamp, 1)

    def week(self, timestamp):
        ''' Get a timespan for the running week. '''
        return weeutil.weeutil.archiveWeekSpan(timestamp, startOfWeek=self.week_start)

    def month(self, timestamp):
        ''' Get a timespan for the running month. '''
        return weeutil.weeutil.archiveMonthSpan(timestamp)

    def year(self, timestamp):
        ''' Get a timespan for the running year. '''
        return weeutil.weeutil.archiveYearSpan(timestamp)

    def last24hours(self, timestamp):
        ''' Get a timespan for the last 24 hours. '''
        return TimeSpan(timestamp - 86400, timestamp)

    def last7days(self, timestamp):
        ''' Get a timespan for the last 7 days. '''
        return self._last_n_days(7, timestamp)

    def last31days(self, timestamp):
        ''' Get a timespan for the last 31 days. '''
        return self._last_n_days(31, timestamp)

    def last366days(self, timestamp):
        ''' Get a timespan for the last 366 days. '''
        return self._last_n_days(366, timestamp)

    def _last_n_days(self, days, timestamp):
        """ Get a TimeSpan for the last N days """
        return TimeSpan(time.mktime((datetime.date.fromtimestamp(timestamp) - datetime.timedelta(days=days)).timetuple()), timestamp)

    def shift_timespan(self, current_timespan):
        """ Shift the start/stop time by since_seconds """
        return TimeSpan(current_timespan.start + self.since_seconds, current_timespan.stop + self.since_seconds)

    def since(self, agg_dict, timestamp, orig_timestamp=None):
        """ Get a TimeSpan for offset of since_hours from midnight """

        if orig_timestamp is None:
            if to_bool(agg_dict.get("yesterday", False)):
                timestamp -= 86400
                orig_timestamp = timestamp
            else:
                orig_timestamp = timestamp

        if to_bool(agg_dict.get("yesterday", False)):
            timespan = weeutil.weeutil.archiveDaySpan(timestamp)
        elif to_bool(agg_dict.get("week", False)):
            timespan = weeutil.weeutil.archiveWeekSpan(timestamp, startOfWeek=self.week_start)
        elif to_bool(agg_dict.get("month", False)):
            timespan = weeutil.weeutil.archiveMonthSpan(timestamp)
        elif to_bool(agg_dict.get("year", False)):
            timespan = weeutil.weeutil.archiveYearSpan(timestamp)
        else:
            timespan = weeutil.weeutil.archiveDaySpan(timestamp)

        if self.since_seconds > 0:
            timespan = self.shift_timespan(timespan)

        if timespan.start <= orig_timestamp <= timespan.stop:
            return timespan

        return self.since(agg_dict, timestamp - 86400, orig_timestamp)

class AggregatedValuesService(StdService):
    """ A service to publish WeeWX loop and/or archive data to MQTT. """
    def __init__(self, engine, config_dict):
        super().__init__(engine, config_dict)

        self.version = "1.0.0"

        self.logger = Logger()

        self.process_config_dict(config_dict)

        self.logger.loginf(f"AggregatedValues version: {self.version}")

        service_dict = config_dict.get('AggregatedValues', {})

        self.logger.logdbg(f"service_dict is {service_dict}")

        self.enable = to_bool(service_dict.get('enable', True))
        if not self.enable:
            self.logger.loginf("Not enabled, exiting.")
            return

        data_binding = service_dict.get('data_binding', 'wx_binding')

        self.manager_dict = weewx.manager.get_manager_dict_from_config(config_dict, data_binding)

        with weewx.manager.open_manager(self.manager_dict) as db_manager:
            self.db_manager = db_manager

        self.timespan_provider = TimeSpanProvider(engine.stn_info.week_start, int(service_dict.get("since_hour", 0)))

        self.fields = self.configure_fields(service_dict)

        self.bind(weewx.NEW_ARCHIVE_RECORD, self.new_archive_record)

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
            ignore = to_bool(field_dict.get('ignore', False))
            if ignore:
                continue

            if field_dict.get('observation') is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have an observation set, skipping...")
                continue

            if field_dict.get('aggregation') is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have an aggregation set, skipping...")
                continue

            if field_dict.get('period') is None:
                self.logger.logerr(f"Error! Field '{field}' doesn't have a period set, skipping...")
                continue

            fields[field] = field_dict

        return fields

    def new_archive_record(self, event):
        record = event.record

        for field in self.fields:

            self.logger.loginf(f"field: {field}")

            field_dict = self.fields[field]

            self.logger.loginf(f"field_dict: {field_dict}")

            try:
                time_span = self.timespan_provider.get_timespan(field_dict, record['dateTime'])

                record[field] = \
                    weewx.xtypes.get_aggregate(field_dict['observation'], time_span, field_dict['aggregation'], self.db_manager)[2]

            except (weewx.CannotCalculate, weewx.UnknownAggregation, weewx.UnknownType) as exception:
                self.logger.logerr(f"Aggregation failed: {exception}")

    def shutDown(self):
        """Run when an engine shutdown is requested."""
        self.logger.loginf("Shutdown initiatead")
