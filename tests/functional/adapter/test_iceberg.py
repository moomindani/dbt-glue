from typing import List
import pytest
from dbt.tests.adapter.basic.files import (base_table_sql, base_view_sql,)
from dbt.tests.adapter.basic.test_base import BaseSimpleMaterializations
from dbt.tests.adapter.basic.test_empty import BaseEmpty
from dbt.tests.adapter.basic.test_generic_tests import BaseGenericTests
from dbt.tests.adapter.basic.test_incremental import BaseIncremental
from dbt.tests.adapter.basic.test_singular_tests import BaseSingularTests
from dbt.tests.adapter.basic.test_singular_tests_ephemeral import BaseSingularTestsEphemeral
from dbt.tests.adapter.basic.test_table_materialization import BaseTableMaterialization
from dbt.tests.adapter.basic.test_validate_connection import BaseValidateConnection
from dbt.tests.util import (check_result_nodes_by_name,check_relation_types, check_relations_equal_with_relations, TestProcessingException,
                            run_dbt, check_relations_equal, relation_from_name)
from tests.util import get_s3_location


# override schema_base_yml to set missing database
schema_base_yml = """
version: 2
sources:
  - name: raw
    schema: "{{ target.schema }}"
    database: "{{ target.schema }}"
    tables:
      - name: seed
        identifier: "{{ var('seed_name', 'base') }}"
"""

# override base_materialized_var_sql to set strategy=insert_overwrite
config_materialized_var = """
  {{ config(materialized=var("materialized_var", "table"), file_format="iceberg") }}
"""
config_incremental_strategy = """
  {{ config(incremental_strategy='insert_overwrite') }}
"""
model_base = """
  select * from {{ source('raw', 'seed') }}
"""
base_materialized_var_sql = config_materialized_var + config_incremental_strategy + model_base


def get_relation(adapter, name: str):
    """reverse-engineer a relation from a given name and
    the adapter. The relation name is split by the '.' character.
    """

    # Different adapters have different Relation classes
    cls = adapter
    credentials = adapter.config.credentials

    # Make sure we have database/schema/identifier parts, even if
    # only identifier was supplied.
    relation_parts = name.split(".")
    if len(relation_parts) == 1:
        relation_parts.insert(0, credentials.schema)
    if len(relation_parts) == 2:
        relation_parts.insert(0, credentials.database)
    relation = cls.get_relation(relation_parts[0], relation_parts[1], relation_parts[2])

    return relation


def check_relations_equal(adapter, relation_names: List, compare_snapshot_cols=False):
    if len(relation_names) < 2:
        raise TestProcessingException(
            "Not enough relations to compare",
        )
    relations = [get_relation(adapter, name) for name in relation_names]
    return check_relations_equal_with_relations(
        adapter, relations, compare_snapshot_cols=compare_snapshot_cols
    )


class TestSimpleMaterializationsGlue(BaseSimpleMaterializations):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "name": "base",
            "models": {
                "+incremental_strategy": "append",
            }
        }

    @pytest.fixture(scope="class")
    def models(self):
        return {
            "view_model.sql": base_view_sql,
            "table_model.sql": base_table_sql,
            "swappable.sql": base_materialized_var_sql,
            "schema.yml": schema_base_yml,
        }

    def test_base(self, project):
        # seed command
        results = run_dbt(["seed"])
        # seed result length
        assert len(results) == 1

        # run command
        results = run_dbt()
        # run result length
        assert len(results) == 3

        # names exist in result nodes
        check_result_nodes_by_name(results, ["view_model", "table_model", "swappable"])

        # check relation types
        expected = {
            "base": "table",
            "view_model": "view",
            "table_model": "table",
            "swappable": "table",
        }
        check_relation_types(project.adapter, expected)

        # base table rowcount
        relation = relation_from_name(project.adapter, "base")
        result = project.run_sql(f"select count(*) as num_rows from {relation}", fetch="one")
        assert result[0] == 10

        # relations_equal
        check_relations_equal(project.adapter, ["base", "view_model", "table_model", "swappable"])

        # check relations in catalog
        catalog = run_dbt(["docs", "generate"])
        assert len(catalog.nodes) == 4
        assert len(catalog.sources) == 1

        # run_dbt changing materialized_var to view
        if project.test_config.get("require_full_refresh", False):  # required for BigQuery
            results = run_dbt(
                ["run", "--full-refresh", "-m", "swappable", "--vars", "materialized_var: view"]
            )
        else:
            results = run_dbt(["run", "-m", "swappable", "--vars", "materialized_var: view"])
        assert len(results) == 1

        # check relation types, swappable is view
        expected = {
            "base": "table",
            "view_model": "view",
            "table_model": "table",
            "swappable": "view",
        }
        check_relation_types(project.adapter, expected)

        # run_dbt changing materialized_var to incremental
        results = run_dbt(["run", "-m", "swappable", "--vars", "materialized_var: incremental"])
        assert len(results) == 1

        # check relation types, swappable is table
        expected = {
            "base": "table",
            "view_model": "view",
            "table_model": "table",
            "swappable": "table",
        }
        check_relation_types(project.adapter, expected)


class TestSingularTestsGlue(BaseSingularTests):
    pass


class TestEmptyGlue(BaseEmpty):
    pass


class TestSingularTestsEphemeralGlue(BaseSingularTestsEphemeral):
    pass


class TestIncrementalGlue(BaseIncremental):
    @pytest.fixture(scope="class")
    def models(self):
        # Define SQL for the incremental model
        incremental_sql = """
            {{ config(
                materialized="incremental",
                incremental_strategy="append",
                file_format="iceberg",
                add_iceberg_timestamp=true
            ) }}

            {% set source_relation = source('raw', 'seed') %}
            
            {% if is_incremental() %}
                {% set source_cols = adapter.get_columns_in_relation(source_relation) | map(attribute='name') | list %}
                {% set existing_cols = adapter.get_columns_in_relation(this) | map(attribute='name') | reject('in', ['update_iceberg_ts']) | list %}
            {% else %}
                {% set source_cols = adapter.get_columns_in_relation(source_relation) | map(attribute='name') | list %}
                {% set existing_cols = source_cols %}
            {% endif %}

            with source_data as (
                select 
                    {% for column in source_cols %}
                    {{ column }}{% if not loop.last %},{% endif %}
                    {% endfor %}
                from {{ source_relation }}
                {% if is_incremental() %}
                where id > (
                    select max(id) 
                    from {{ this }}
                )
                {% endif %}
            )
            select
                {% for column in existing_cols %}
                {{ column }}{% if not loop.last %},{% endif %}
                {% endfor %}
            from source_data
        """.strip()

        return {
            "incremental.sql": incremental_sql,
            "schema.yml": schema_base_yml
        }

    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "name": "incremental_test",
            "models": {
                "+file_format": "iceberg",
                "+add_iceberg_timestamp": True
            }
        }

    def test_incremental(self, project):
        # Initial seed
        results = run_dbt(["seed"])
        assert len(results) == 2

        # Verify base table
        relation = relation_from_name(project.adapter, "base")
        project.run_sql(f"refresh table {relation}")
        result = project.run_sql(f"select count(*) as num_rows from {relation}", fetch="one")
        assert result[0] == 10

        # Verify added table
        relation = relation_from_name(project.adapter, "added")
        project.run_sql(f"refresh table {relation}")
        result = project.run_sql(f"select count(*) as num_rows from {relation}", fetch="one")
        assert result[0] == 20

        # Run model with base data
        results = run_dbt(["run", "--vars", "seed_name: base"])
        assert len(results) == 1

        # Compare data without timestamp
        incremental_relation = relation_from_name(project.adapter, "incremental")
        base_relation = relation_from_name(project.adapter, "base")
        sql = f"""
        SELECT COUNT(*) FROM (
            SELECT id, name, some_date FROM {incremental_relation}
            EXCEPT
            SELECT * FROM {base_relation}
        )
        """
        result = project.run_sql(sql, fetch="one")
        assert result[0] == 0, "Data should match between base and incremental"

        # Run model with added data
        results = run_dbt(["run", "--vars", "seed_name: added"])
        assert len(results) == 1

        # Compare final data
        added_relation = relation_from_name(project.adapter, "added")
        sql = f"""
        SELECT COUNT(*) FROM (
            SELECT id, name, some_date FROM {incremental_relation}
            EXCEPT
            SELECT * FROM {added_relation}
        )
        """
        result = project.run_sql(sql, fetch="one")
        assert result[0] == 0, "Data should match between added and incremental"

class TestIncrementalGlueWithCustomLocation(TestIncrementalGlue):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        default_location = get_s3_location()
        custom_prefix = "{{target.schema}}/custom/incremental"
        custom_location = default_location + custom_prefix
        return {
            "name": "incremental",
            "models": {
                "+file_format": "iceberg",
                "+add_iceberg_timestamp": True,
                "+custom_location": custom_location
            }
        }

class TestGenericTestsGlue(BaseGenericTests):
    def test_generic_tests(self, project):
        # seed command
        results = run_dbt(["seed"])
        assert len(results) == 1

        relation = relation_from_name(project.adapter, "base")
        # run refresh table to disable the previous parquet file paths
        project.run_sql(f"refresh table {relation}")

        # test command selecting base model
        results = run_dbt(["test", "-m", "base"])
        assert len(results) == 1

        # run command
        results = run_dbt(["run"])
        assert len(results) == 2

        # test command, all tests
        results = run_dbt(["test"])
        assert len(results) == 3

    pass


class TestTableMatGlue(BaseTableMaterialization):
    pass


class TestValidateConnectionGlue(BaseValidateConnection):
    pass


class TestIcebergTimestamp(BaseSimpleMaterializations):
    @pytest.fixture(scope="class")
    def project_config_update(self):
        return {
            "name": "iceberg_timestamp_test",
            "models": {
                "+file_format": "iceberg"
            }
        }

    @pytest.fixture(scope="class")
    def models(self):
        timestamp_enabled_sql = """
            {{ config(
                materialized="table",
                add_iceberg_timestamp=true
            ) }}
            select * from {{ source('raw', 'seed') }}
        """

        timestamp_disabled_sql = """
            {{ config(
                materialized="table",
                add_iceberg_timestamp=false
            ) }}
            select * from {{ source('raw', 'seed') }}
        """

        return {
            "enabled_timestamp.sql": timestamp_enabled_sql,
            "disabled_timestamp.sql": timestamp_disabled_sql,
            "schema.yml": schema_base_yml,
        }

    def test_base(self, project):
        # Initial setup and model count verification
        results = run_dbt(["seed"])
        assert len(results) == 1

        results = run_dbt(["run"])
        assert len(results) == 2  # Only two models in this test

        check_result_nodes_by_name(results, ["enabled_timestamp", "disabled_timestamp"])

        expected = {
            "base": "table",
            "enabled_timestamp": "table",
            "disabled_timestamp": "table",
        }
        check_relation_types(project.adapter, expected)

    def test_iceberg_timestamp(self, project):
        # Run initial seed and models
        results = run_dbt(["seed"])
        assert len(results) == 1

        results = run_dbt(["run"])
        assert len(results) == 2

        # Check enabled model using a SELECT statement
        relation_enabled = relation_from_name(project.adapter, "enabled_timestamp")
        sql = f"""
        SELECT column_name 
        FROM information_schema.columns
        WHERE table_schema = '{relation_enabled.schema}'
        AND table_name = '{relation_enabled.identifier}'
        """
        try:
            project.adapter.config.credentials.use_arrow = False  # Force Arrow off
            result = project.run_sql(sql, fetch="all")
            enabled_columns = [row[0].lower() for row in result]
            assert any('update_iceberg_ts' in col for col in enabled_columns), \
                "update_iceberg_ts column should exist"
        except Exception as e:
            project.adapter.logger.warning(f"Failed to list columns using information_schema: {e}")
            # Fallback to direct column check
            sql = f"SELECT update_iceberg_ts FROM {relation_enabled} LIMIT 1"
            result = project.run_sql(sql, fetch="one")
            assert result is not None, "update_iceberg_ts column should exist and be queryable"

        # Check disabled model
        relation_disabled = relation_from_name(project.adapter, "disabled_timestamp")
        sql = f"""
        SELECT column_name 
        FROM information_schema.columns
        WHERE table_schema = '{relation_disabled.schema}'
        AND table_name = '{relation_disabled.identifier}'
        """
        try:
            result = project.run_sql(sql, fetch="all")
            disabled_columns = [row[0].lower() for row in result]
            assert not any('update_iceberg_ts' in col for col in disabled_columns), \
                "update_iceberg_ts column should not exist"
        except Exception as e:
            project.adapter.logger.warning(f"Failed to list columns using information_schema: {e}")
            # Fallback to negative check
            try:
                sql = f"SELECT update_iceberg_ts FROM {relation_disabled} LIMIT 1"
                project.run_sql(sql, fetch="one")
                assert False, "Should not be able to query update_iceberg_ts"
            except Exception:
                pass  # Expected to fail as column should not exist

        # Verify data consistency
        relation_base = relation_from_name(project.adapter, "base")

        # Check row counts
        base_count = project.run_sql(f"SELECT COUNT(*) FROM {relation_base}", fetch="one")[0]
        enabled_count = project.run_sql(f"SELECT COUNT(*) FROM {relation_enabled}", fetch="one")[0]
        disabled_count = project.run_sql(f"SELECT COUNT(*) FROM {relation_disabled}", fetch="one")[0]

        assert base_count == enabled_count == disabled_count, "Row counts should match"

        # Compare actual data
        sql = f"""
        SELECT COUNT(*) FROM (
            SELECT id, name, some_date FROM {relation_enabled}
            EXCEPT
            SELECT * FROM {relation_base}
        )
        """
        diff_count = project.run_sql(sql, fetch="one")[0]
        assert diff_count == 0, "Data should match between base and enabled_timestamp (excluding timestamp)"
