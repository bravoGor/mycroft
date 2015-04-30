# -*- coding: utf-8 -*-
from datetime import datetime
import os
import sys
import time
import traceback
import yaml

from mrjob.job import MRJob
from mrjob.protocol import RawProtocol

from sherlock.common.util import load_from_file
from sherlock.common.protocols import RedshiftExportProtocol
from sherlock.common.redshift_schema import RedShiftLogSchema
from sherlock.common.schema import Column
from sherlock.common.schema import Table
from mrjob.runner import log


def derive_filename(name_to_object, context=None):
    return os.environ['map_input_file']


class MRRedshiftETL(MRJob):

    OUTPUT_PROTOCOL = RawProtocol
    HADOOP_OUTPUT_FORMAT = 'oddjob.MultipleValueOutputFormat'

    def configure_options(self):
        super(MRRedshiftETL, self).configure_options()
        self.add_file_option(
            '--extractions', type='string', default="schema/db.yaml",
            help="Yaml file specifies what information to extract "
            "(default: %default)")
        self.add_passthrough_option(
            "--column-delimiter", type='string', default='|',
            help="column delimiter",
        )

    def mapper_init(self):
        """ mrjob initialization.
        Should you decide to override, you should call 'super' to invoke
        this method.
        """
        yaml_data = load_from_file(self.options.extractions)
        schema = RedShiftLogSchema(yaml.load(yaml_data))
        self.schema = schema

        self.table_name_to_columns = dict(
            (table_name,
                [Column.create_from_table(table, column)
                    for column in table['columns']])
            for table_name, table in schema.tables().iteritems()
        )
        self.table_name_to_table = dict(
            (table_name,
                Table.create(table,
                             columns=self.table_name_to_columns[table_name]))
            for table_name, table in schema.tables().iteritems()
        )
        self.table_name_to_output_order = dict(
            (table_name,
                [column.name for column in columns if not column.is_noop])
            for table_name, columns in self.table_name_to_columns.iteritems()
        )
        self.redshift_export = RedshiftExportProtocol(
            delimiter=self.options.column_delimiter
        )

        error_table_name, error_table = self.schema.get_error_table()
        self.error_tbl_name = error_table_name
        self.error_tbl_output_order = [c['log_key'] for c in error_table['columns']]

    def mapper_final(self):
        for table_name in self.table_name_to_table:
            table = self.table_name_to_table[table_name]
            if len(table.truncated_columns):
                log.info("{0}: truncated columns {1}".format(
                    table_name, table.truncated_columns
                ))

    def derive_metric(
        self, name_to_object=None, metric_name=None, context=None
    ):
        """define this in your own class to support derived field

        For example, if you want to generate a derived field called
        odd_or_even for a boolean column.

        name_to_object = {'session': {'random': 77}}

        def derive_metric(
            self, name_to_object=None, metric_name=None, context=None
        ):
          if metric_name == 'odd_or_even':
            return bool(get_deep('session.random') % 2 == 0)
          raise ValueError('I don't recognize this metric')

        name_to_object: a dict in which key is referenced in your schema
          and generated by your mapper, whereas value
          is generated by your mapper.
        metric_name: name of the derived metric
        context: a dict contains the following:
          'self': is the value of parent
          'index': is the index value of an item being processed in an array

        Returns:
          a scalar fit for your derived metric
        """
        raise NotImplementedError

    def emit_exception_row(self, value):
        """Use this inside your mapper function to generate
           error rows for Redshift tables on exception

           value: variable passed to mapper function
        """
        exc_type, exc_value, exc_tb = sys.exc_info()
        error_msg = {
            'crash_tb': ''.join(traceback.format_tb(exc_tb)),
            'crash_exc': traceback.format_exception_only(
                exc_type, exc_value
            )[0].strip()
        }
        now_utc_ms = long(time.mktime(datetime.utcnow().timetuple())) * 1000

        row = {
            'starttime':    now_utc_ms,                 # starttime in UTC millisecs
            'filename':     derive_filename(None),      # source filename with error
            'line_number':  None,                       # line number (not known with mrjob)
            'raw_line':     value,                      # raw line
            'error_reason': error_msg['crash_exc'],     # error summary
            'error_detail': error_msg                   # error detail
        }
        self.redshift_export.given_output_order = self.error_tbl_output_order
        return self.error_tbl_name, self.redshift_export.write(None, row)

    def emit_rows_for_tables(self, name_to_object):
        """Use this inside your mapper function to generate
           rows for Redshift tables

        name_to_object: a dictionary with keys that may be
           referenced by your schema
        """
        for table_name, table in self.table_name_to_table.iteritems():
            for row in table.value_iterator(name_to_object=name_to_object,
                                            derive_metric=self.derive_metric):
                self.redshift_export.given_output_order = \
                    self.table_name_to_output_order[table_name]
                yield table_name, self.redshift_export.write(None, row)