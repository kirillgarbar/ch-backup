"""
Steps for interacting with ClickHouse DBMS.
"""
import yaml
from behave import given, then, when
from hamcrest import assert_that, equal_to, has_length
from tenacity import retry, stop_after_attempt, wait_fixed

from tests.integration.modules.clickhouse import ClickhouseClient


@given('a working clickhouse on {node:w}')
@retry(wait=wait_fixed(0.5), stop=stop_after_attempt(360))
def step_wait_for_clickhouse_alive(context, node):
    """
    Wait until clickhouse is ready to accept incoming requests.
    """
    ClickhouseClient(context, node).ping()


@given('clickhouse on {node:w} has test schema')
@when('clickhouse on {node:w} has test schema')
def step_init_test_schema(context, node):
    """
    Load test schema to clickhouse.
    """
    ClickhouseClient(context, node).init_schema()


@given('{node:w} has test clickhouse data {test_name:w}')
@when('{node:w} has test clickhouse data {test_name:w}')
def step_fill_with_test_data(context, node, test_name):
    """
    Load test data to clickhouse.
    """
    ClickhouseClient(context, node).init_data(mark=test_name)


@given('test data on {node:w} that was created as follows')
def step_test_data(context, node):
    queries = []
    for string in context.text.split(';'):
        string = string.strip()
        if string:
            queries.append(string)

    ch_client = ClickhouseClient(context, node)
    for query in queries:
        ch_client.execute(query)


@given('we have dropped test table #{table_num:d} in db #{db_num:d} on {node}')
@when('we drop test table #{table_num:d} in db #{db_num:d} on {node}')
def step_drop_test_table(context, table_num, db_num, node):
    ClickhouseClient(context, node).drop_test_table(
        db_num=db_num, table_num=table_num)


@then('we got same clickhouse data at {nodes}')
def step_same_clickhouse_data(context, nodes):
    user_data = []
    for node in nodes.split():
        ch_client = ClickhouseClient(context, node)
        _, rows_data = ch_client.get_all_user_data()
        user_data.append(rows_data)

    node1_data = user_data[0]
    for node_num in range(1, len(user_data)):
        node_data = user_data[node_num]
        assert_that(node_data, equal_to(node1_data))


@then('{node1:w} has the subset of {node2:w} data')
def step_has_subset_data(context, node1, node2):
    options = yaml.load(context.text)
    tables = options['tables']

    node_data = {}
    for node in (node1, node2):
        ch_client = ClickhouseClient(context, node)
        _, node_data[node] = ch_client.get_all_user_data()

    assert_that(node_data[node1], has_length(len(tables)))
    for table in tables:
        assert_that(node_data[node1][table], equal_to(node_data[node2][table]))


@when('we drop all databases at {node:w}')
def step_drop_databases(context, node):
    ch_client = ClickhouseClient(context, node)
    for db_name in ch_client.get_all_user_databases():
        ch_client.drop_database(db_name)


@then('{node1:w} has same schema as {node2:w}')
def step_has_same_schema(context, node1, node2):
    def _get_ddl(node):
        ch_client = ClickhouseClient(context, node)
        return ch_client.get_all_user_schemas()

    assert_that(_get_ddl(node1), equal_to(_get_ddl(node2)))


@then('on {node:w} tables are empty')
def step_check_tables_are_empty(context, node):
    ch_client = ClickhouseClient(context, node)
    row_count, _ = ch_client.get_all_user_data()
    assert_that(row_count, equal_to(0))
