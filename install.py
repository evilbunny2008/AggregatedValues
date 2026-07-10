#    Copyright (c) 2025 Rich Bell <bellrichm@gmail.com>
#
#    See the file LICENSE.txt for your full rights.
#

""" Installer for AggregatedValues service.

To uninstall run
weectl extension uninstall -y AggregatedValues
"""

from io import StringIO

import configobj

from weecfg.extension import ExtensionInstaller

VERSION = "1.0.13"

CONFIG = """
[AggregatedValues]
    # Whether the service is enabled or not.
    # Valid values: true or false
    # Default is true.
    enable = false

    # If you want to use an hour other than midnight for the since aggregate type, eg for rain offsets because the readings are reported at 9am
    # since_hour = 9

    # This can be any name. For example: rainSumDay, outTempMinHour, etc
    [[outTemp]]
        # Turn aggregates on and off.
        # Default is true.
        ignore = true

        # The WeeWX observation to aggregate, rain, outTemp, etc,
        observation =

        # The type of aggregation to perform.
        # See, https://www.weewx.com/docs/customizing.htm#aggregation_types
        aggregation =

        # The time period over which the aggregation shoulf occurr.
        # Valid values: hour, day, week, month, year, yesterday, last24hours, last7days, last31days, last366days
        period =
"""

def loader():
    """ Load and return the extension installer. """
    return AggregatedValuesInstaller()


class AggregatedValuesInstaller(ExtensionInstaller):
    """ The extension installer. """
    def __init__(self):

        install_dict = {
            'version': VERSION,
            'name': 'AggregatedValues',
            # add a leading space, so that long versions does not run into the description
            'description': ' Calculate aggregated values that other extensions can then use without duplicating functionality and database lookups',
            'author': "John Smith",
            'author_email': "deltafoxtro256+AggregatedValues@gmail.com",
            'files': [('bin/user', ['bin/user/AggregatedValues.py'])]
        }

        install_dict['config'] = configobj.ConfigObj(StringIO(CONFIG))
        # ToDo: Better service group?
        install_dict['prep_services'] = 'user.AggregatedValues.AggregatedValuesService'

        super().__init__(install_dict)
