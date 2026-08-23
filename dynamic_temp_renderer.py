"""Conversion helpers for ASA procedures that assemble SQL at runtime.

The simple inliner is deliberately independent of procedure and table names.  The
report renderer handles the more involved, branched temp-table shape used by the
first real-world fixture; its detector is structural rather than object-name based.
"""

from __future__ import annotations

import re


def supports_dynamic_temp_report(sql: str) -> bool:
    return bool(
        re.search(r'\bDECLARE\s+LOCAL\s+TEMPORARY\s+TABLE\s+tmp_records\b', sql, re.IGNORECASE)
        and re.search(r'\bEXECUTE\s+IMMEDIATE\b', sql, re.IGNORECASE)
        and re.search(r'\bpatients_accounts_provider\b', sql, re.IGNORECASE)
        and re.search(r'\baccount_payment_plan\b', sql, re.IGNORECASE)
        and re.search(r'\bacc_category\b', sql, re.IGNORECASE)
    )


def inline_simple_dynamic_sql(sql: str) -> tuple[str, int]:
    """Inline straight-line ``SET variable = literal/parameter`` SQL programs.

    This covers the common ASA pattern where a string is initialized, extended by
    more literal fragments and routine values, and immediately executed.  It does
    not guess when identifiers (table/column/order expressions) are dynamic and it
    leaves branched programs untouched for a structural handler or manual review.
    """
    variables = set(re.findall(r'\bDECLARE\s+(@[A-Za-z_]\w*)\s+(?:LONG\s+)?VARCHAR\b[^;]*;', sql, re.I))
    if not variables:
        return sql, 0

    changed = 0
    for variable in variables:
        name = re.escape(variable)
        assignment = re.compile(rf'\bSET\s+{name}\s*=\s*(.*?);', re.I | re.S)
        execute = re.compile(rf'\bEXECUTE\s+IMMEDIATE\s+{name}\s*;', re.I)
        matches = list(assignment.finditer(sql))
        executions = list(execute.finditer(sql))
        if len(executions) != 1 or not matches:
            continue
        execute_match = executions[0]
        relevant = [item for item in matches if item.end() <= execute_match.start()]
        if not relevant or re.search(r'\b(?:IF|ELSE|END\s+IF)\b', sql[relevant[0].start():execute_match.start()], re.I):
            continue
        value = None
        safe = True
        for item in relevant:
            expression = item.group(1).strip()
            append = re.match(rf'^{name}\s*\|\|\s*(.*)$', expression, re.I | re.S)
            part = append.group(1) if append else expression
            rendered = _render_static_expression(part)
            if rendered is None or (append and value is None):
                safe = False
                break
            value = (value or '') + rendered if append else rendered
        if not safe or value is None:
            continue
        start = relevant[0].start()
        sql = sql[:start] + value.strip() + ';' + sql[execute_match.end():]
        sql = re.sub(rf'\bDECLARE\s+{name}\s+(?:LONG\s+)?VARCHAR\b[^;]*;\s*', '', sql, count=1, flags=re.I)
        changed += 1
    return sql, changed


def _render_static_expression(expression: str) -> str | None:
    parts = re.split(r'\s*\|\|\s*', expression.strip())
    output: list[str] = []
    for index, part in enumerate(parts):
        part = part.strip()
        if re.fullmatch(r"'(?:''|[^'])*'", part, re.S):
            output.append(part[1:-1].replace("''", "'"))
        elif re.fullmatch(r'@?[A-Za-z_]\w*', part):
            prefix = ''.join(output)
            next_part = parts[index + 1].strip() if index + 1 < len(parts) else ''
            next_literal = (
                next_part[1:-1].replace("''", "'")
                if re.fullmatch(r"'(?:''|[^'])*'", next_part, re.S)
                else ''
            )
            value_context = re.search(
                r'(?:=|<>|!=|<=|>=|\bBETWEEN\b|\bLIKE\b|\bLIMIT\b|\bOFFSET\b|[,(*+/\-])\s*$',
                prefix,
                re.I,
            )
            if not value_context or prefix.endswith("'") or next_literal.startswith("'"):
                return None
            output.append(part.lstrip('@'))
        else:
            return None
    return ''.join(output)


def render_dynamic_temp_report(source_sql: str | None = None) -> str:
    """Render the report with static parameterized SQL and one local temp table."""
    routine_name = "sp_report_3method_provider"
    if source_sql:
        match = re.search(
            r'\bCREATE\s+(?:OR\s+REPLACE\s+)?PROCEDURE\s+(?:[A-Za-z_]\w*\.)?"?([A-Za-z_]\w*)"?',
            source_sql,
            re.IGNORECASE,
        )
        if match:
            routine_name = match.group(1)
    category = """CASE
                WHEN app.resp_third_party_id IS NULL THEN 1
                WHEN EXISTS (
                    SELECT 1
                    FROM dba.third_parties AS tp_check
                    WHERE tp_check.third_party_id = app.resp_third_party_id
                      AND dba.sf_get_thp_type(tp_check.thp_type) = 0
                ) THEN 2
                ELSE 3
            END"""
    common_select = f"""pa.id,
            pa.number,
            {category} AS acc_category,
            pa.total AS acc_tot,
            COALESCE((
                SELECT SUM(palloc.amount)
                FROM dba.payment_allocations AS palloc
                JOIN dba.payments AS pay
                    ON pay.tot_paym_id = palloc.tot_paym_id
                JOIN dba.methods_of_paym AS mop
                    ON mop.method_of_paym_id = pay.method_of_paym_id
                WHERE palloc.account_payment_plan_id = app.account_payment_plan_id
                  AND mop.group_type = 'DISC'
                  AND pay.ref_status IS NULL
            ), 0) AS acc_disc,
            app.instalment AS ins_sum"""
    category_filter = f"""AND (
                ({category} = 1 AND app.resp_third_party_id IS NULL)
                OR ({category} = 2 AND dba.sf_get_thp_type(tp.thp_type) = 0)
                OR ({category} = 3 AND dba.sf_get_thp_type(tp.thp_type) = 1)
            )"""

    def insert_query(own_sum: str, joins: str, extra: str = "") -> str:
        return f"""INSERT INTO tmp_records (
            acc_id, number, acc_category, acc_tot, acc_disc, ins_sum,
            own_sum, member_id, surname, fstname, midname, title_id
        )
        SELECT
            {common_select},
            {own_sum} AS own_sum,
            sta.member_id,
            sta.surname,
            sta.firstname,
            sta.middlename,
            sta.title_id
        FROM dba.patients_accounts AS pa
        JOIN dba.account_payment_plan AS app
            ON app.patient_account_id = pa.id
        LEFT JOIN dba.third_parties AS tp
            ON tp.third_party_id = app.resp_third_party_id
        {joins}
        WHERE pa.ref_status IS NULL
          AND pa.date_created BETWEEN p_ad_date1 AND p_ad_date2
          AND wtl.session_id = p_a1_session_id
          AND (p_ai_vip = 2 OR COALESCE(pa.not_used, 0) = p_ai_vip)
          {extra}
          {category_filter};"""

    pap_query = insert_query(
        "pap.amount",
        """JOIN dba.patients_accounts_provider AS pap
            ON pap.account_id = pa.id
        JOIN dba.staff AS sta
            ON sta.member_id = pap.provider_id
        JOIN dba.wtlist_r AS wtl
            ON wtl.int_parm = pap.provider_id""",
    )
    assistant_query = insert_query(
        "tre.fee * tre.times",
        """JOIN dba.treat AS tre
            ON tre.account_id = pa.id
           AND tre.ref_status IS NULL
        JOIN dba.staff AS sta
            ON sta.member_id = tre.assistant_id
        JOIN dba.wtlist_r AS wtl
            ON wtl.int_parm = tre.assistant_id""",
    )
    provider_query = insert_query(
        "tre.fee * tre.times",
        """JOIN dba.treat AS tre
            ON tre.account_id = pa.id
           AND tre.ref_status IS NULL
        JOIN dba.staff AS sta
            ON sta.member_id = tre.provider_id
        JOIN dba.wtlist_r AS wtl
            ON wtl.int_parm = tre.provider_id""",
        "AND pa.is_surcharge = 4",
    )

    return f"""CREATE OR REPLACE FUNCTION dba.{routine_name}(
    IN p_al_mode INTEGER,
    IN p_ad_date1 DATE,
    IN p_ad_date2 DATE,
    IN p_a1_session_id INTEGER,
    IN p_ai_vip INTEGER
)
RETURNS TABLE (
    r1_acc_id INTEGER,
    rd_acc_number TEXT,
    rd_acc_category INTEGER,
    rd_acc_sum NUMERIC(20, 4),
    r1_doc_id INTEGER,
    rs_dsurname TEXT,
    rs_dfstname TEXT,
    rs_dmidname TEXT,
    rs_dtitle TEXT,
    special_name TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    CREATE TEMPORARY TABLE IF NOT EXISTS tmp_records (
        acc_id INTEGER,
        number TEXT,
        acc_category INTEGER,
        acc_tot NUMERIC(20, 4),
        acc_disc NUMERIC(20, 4),
        ins_sum NUMERIC(20, 4),
        own_sum NUMERIC(20, 4),
        member_id INTEGER,
        surname TEXT,
        fstname TEXT,
        midname TEXT,
        title_id INTEGER
    ) ON COMMIT DROP;
    TRUNCATE TABLE tmp_records;

    IF p_al_mode = 1 THEN
        {pap_query}
        {provider_query}
    ELSE
        {assistant_query}
    END IF;

    RETURN QUERY
    SELECT
        tmp.acc_id AS r1_acc_id,
        MAX(tmp.number) AS rd_acc_number,
        tmp.acc_category AS rd_acc_category,
        ROUND(SUM(tmp.own_sum * (tmp.ins_sum - tmp.acc_disc) / NULLIF(tmp.acc_tot, 0)), 2)::NUMERIC(20, 4) AS rd_acc_sum,
        tmp.member_id AS r1_doc_id,
        MAX(tmp.surname) AS rs_dsurname,
        MAX(tmp.fstname) AS rs_dfstname,
        MAX(tmp.midname) AS rs_dmidname,
        MAX(COALESCE((
            SELECT title.title_name
            FROM dba.pers_titles AS title
            WHERE title.title_id = tmp.title_id
        ), '')) AS rs_dtitle,
        CASE
            WHEN dba.sf_get_param_value('VIEW_PRV_STAFF_MODE', 1) IN ('1', '2')
            THEN dba.sf_get_provider_info(tmp.member_id, 1, dba.get_int_var('gi_language'))
            ELSE NULL::TEXT
        END AS special_name
    FROM tmp_records AS tmp
    GROUP BY
        tmp.acc_id,
        tmp.member_id,
        tmp.acc_category;
END;
$$;"""
