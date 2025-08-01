#!/usr/bin/python
#
# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Lint as: python3
"""Common utilities for Logica predicate compilation and execution."""

import os
import subprocess
import sys

if '.' not in __package__:
  from common import duckdb_logica
  from common import sqlite3_logica
  from common import psql_logica
  from compiler import functors
  from compiler import rule_translate
  from compiler import universe
  from parser_py import parse
  from type_inference.research import infer
else:
  from ..common import duckdb_logica
  from ..common import sqlite3_logica
  from ..common import psql_logica
  from ..compiler import functors
  from ..compiler import rule_translate
  from ..compiler import universe
  from ..parser_py import parse
  from ..type_inference.research import infer


def ParseOrExit(filename, import_root=None):
  """Parse a Logica program."""
  with open(filename) as f:
    program_text = f.read()

  try:
    parsed_rules = parse.ParseFile(program_text,
                                   import_root=import_root)['rule']
  except parse.ParsingException as parsing_exception:
    parsing_exception.ShowMessage()
    sys.exit(1)

  return parsed_rules


def GetProgramOrExit(filename, user_flags=None, import_root=None):
  """Get program object from a file."""
  parsed_rules = ParseOrExit(filename, import_root=import_root)
  try:
    p = universe.LogicaProgram(parsed_rules, user_flags=user_flags)
  except rule_translate.RuleCompileException as rule_compilation_exception:
    rule_compilation_exception.ShowMessage()
    sys.exit(1)
  except functors.FunctorError as functor_exception:
    functor_exception.ShowMessage()
    sys.exit(1)
  return p


def RunQuery(sql,
             settings=None,
             output_format='pretty', engine='bigquery'):
  """Run a SQL query on BigQuery."""
  settings = settings or {}
  if engine == 'psql' and os.environ.get('LOGICA_PSQL_CONNECTION'):
    connection_str = os.environ.get('LOGICA_PSQL_CONNECTION')
    import psycopg2
    from common import psql_logica
    connection = psycopg2.connect(connection_str)
    cursor = psql_logica.PostgresExecute(sql, connection)
    rows = [list(map(psql_logica.DigestPsqlType, row))
        
            for row in cursor.fetchall()]
    return sqlite3_logica.ArtisticTable([d[0] for d in cursor.description],
                                        rows)
  if engine == 'bigquery':
    p = subprocess.Popen(['bq', 'query',
                          '--use_legacy_sql=false',
                          '--format=%s' % output_format],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
  elif engine == 'sqlite':
    # TODO: Make multi-statement scripts work.
    return sqlite3_logica.RunSQL(sql)
  elif engine == 'psql':
    p = subprocess.Popen(['psql', '--quiet'],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE)
  elif engine == 'trino':
    catalog = settings.get('catalog', 'memory')
    server = settings.get('server', 'http://localhost:8080')
    p = subprocess.Popen(['trino',
                          '--catalog=%s' % catalog,
                          '--server=%s' % server] +
                          ['--output-format=ALIGNED'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
  elif engine == 'presto':
    catalog = settings.get('catalog', 'memory')
    server = settings.get('server', 'http://localhost:8080')
    p = subprocess.Popen(['presto',
                          '--catalog=%s' % catalog,
                          '--server=%s' % server,
                          '--file=/dev/stdin'] +
                          ['--output-format=ALIGNED'],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE)
  elif engine == 'duckdb':
    import duckdb
    connection = duckdb.connect()
    if 'clingo' in settings and settings['clingo']:
      duckdb_logica.ConnectClingo(connection)
    df = connection.sql(sql).df()
    return sqlite3_logica.DataframeAsArtisticTable(df)
  else:
    assert False, 'Unknown engine: %s' % engine
  o, _ = p.communicate(sql.encode())
  return o.decode()


def RunPredicate(filename, predicate,
                 output_format='pretty', user_flags=None,
                 import_root=None):
  """Run a predicate on BigQuery."""
  p = GetProgramOrExit(filename, user_flags=user_flags,
                       import_root=import_root)
  sql = p.FormattedPredicateSql(predicate)
  engine = p.annotations.Engine()
  if ('@Engine' in p.annotations.annotations and
      engine in p.annotations.annotations['@Engine']):
    settings = p.annotations.annotations['@Engine'][engine]
  else:
    settings = {}
  return RunQuery(sql, settings,
                  output_format, engine=engine)


def RunQueryPandas(sql, engine, connection=None):
  """Running SQL query on the engine, returning Pandas dataframe."""
  import pandas
  if connection is None and engine == 'sqlite':
    connection = sqlite3_logica.SqliteConnect()
  if connection is None and engine == 'duckdb':
    import duckdb
    connection = duckdb.connect()
  if connection is None:
    assert False, 'Connection is required for engines other than SQLite.'
  if engine == 'bigquery':
    return connection.query(sql).to_dataframe()
  elif engine == 'psql':
    cursor = connection.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    df = pandas.DataFrame(
      rows, columns=[d[0] for d in cursor.description])
    df = df.applymap(psql_logica.DigestPsqlType)
    return df
  elif engine == 'duckdb':
    import duckdb
    return connection.sql(sql).df()
  elif engine == 'sqlite':
    statements = parse.SplitRaw(sql, ';')[:-1]
    if len(statements) > 1:
      connection.executescript(';\n'.join(statements[:-1]))
    return pandas.read_sql(statements[-1], connection)
  else:
    raise Exception('Logica only supports BigQuery, PostgreSQL and SQLite '
                    'for now.')


def RunPredicateToPandas(filename, predicate,
                         user_flags=None, import_root=None, connection=None):
  p = GetProgramOrExit(filename, user_flags=user_flags,
                       import_root=import_root)
  sql = p.FormattedPredicateSql(predicate)
  engine = p.annotations.Engine()
  return RunQueryPandas(sql, engine, connection=connection)


class SqlReceiver:
  def __init__(self):
    self.sql = None


def HandleException(exception):
  sys.exit(1)  # LOL


def CompilePredicateFromString(logica_string,
                               predicate_name,
                               user_flags=None):
  try:
    rules = parse.ParseFile(logica_string)['rule']
  except parse.ParsingException as parsing_exception:
    parsing_exception.ShowMessage()
    return HandleException(parsing_exception)

  try:
    program = universe.LogicaProgram(rules, user_flags=user_flags)
    sql = program.FormattedPredicateSql(predicate_name)
    engine = program.execution.annotations.Engine()
  except rule_translate.RuleCompileException as rule_compilation_exception:
    rule_compilation_exception.ShowMessage()
    return HandleException(rule_compilation_exception)
  except functors.FunctorError as functor_exception:
    functor_exception.ShowMessage()
    return HandleException(functor_exception)
  except infer.TypeErrorCaughtException as type_error_exception:
    type_error_exception.ShowMessage()
    return HandleException(type_error_exception)
  except parse.ParsingException as parsing_exception:
    parsing_exception.ShowMessage()
    return HandleException(parsing_exception)
  return sql, engine


def RunPredicateFromString(logica_string,
                           predicate_name,
                           connection=None,
                           user_flags=None,
                           sql_receiver: SqlReceiver = None):
  sql, engine = CompilePredicateFromString(logica_string, predicate_name,
                                           user_flags)
  if sql_receiver:
    sql_receiver.sql = sql

  return RunQueryPandas(sql, engine, connection)