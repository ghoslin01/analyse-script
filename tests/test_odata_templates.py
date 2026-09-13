import json
from pathlib import Path
import shutil

import pytest

from ifp_contract.cli import main
from ifp_contract.config import load_rule_config
from ifp_contract.extractor import build_contracts, scan_integrator
from ifp_contract.store import ContractStore
from ifp_contract.templates import template_evidence
from ifp_contract.trace import Trace


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
PROFILE = EXAMPLES / 'temenos-odata-config.json'
SAMPLE = EXAMPLES / 'odata-conditional-read.ifp'


def fixture(root):
    shutil.copyfile(SAMPLE, root / SAMPLE.name)
    return root / SAMPLE.name


def test_synthetic_read_rule_contract_in_memory_and_database(tmp_path):
    path = fixture(tmp_path)
    config = load_rule_config(PROFILE)
    operation, = scan_integrator(path, config).operations
    assert operation['classification'] == 'CONFIRMED'
    assert operation['action'] == 'GET'
    assert operation['phase_name'] == 'Lookup.ConditionalRead'
    assert operation['rule_type'] == 'PostPhase'
    assert operation['base_url'] == '$$Request[1].ServiceUri$'
    assert operation['output_group'] == 'Response[1].Records[C]'
    assert '$orderby=DisplayName asc' in operation['filter_expr']
    assert '&$filter=' in operation['filter_expr'] and '&amp;' not in operation['filter_expr']
    assert '\n' in operation['api_path']
    assert operation['attributes']['HTTPHeaderValue'] == '$$Request[1].HeaderValue$'
    store = ContractStore(tmp_path / 'contracts.db')
    try:
        build_contracts(tmp_path, path, path.name, store, rule_config=config)
        stored, = store.rows('SELECT action, base_url, output_group, filter_expr FROM odata_operations')
        assert dict(stored) == {k: operation[k] for k in stored.keys()}
    finally:
        store.close()
    report = tmp_path / 'report.md'
    assert main(['report', '--db', str(tmp_path / 'contracts.db'), '--output', str(report)]) == 0
    text = report.read_text()
    assert 'API request template' in text
    assert text.count('Path candidate:') == 3
    assert 'HeaderValue' in text and 'Rule scheduling configuration: `PostPhase`' in text


def test_trace_preserves_all_request_dependencies_and_path_priority(tmp_path):
    fixture(tmp_path)
    data = Trace(tmp_path, rules_config=PROFILE).collect(SAMPLE.name, 'Response[1].Records[1].Name', None,
                                                        'Lookup.ConditionalRead')
    api, = [e for e in data['events'] if e['kind'] == 'api']
    assert data['seeds'] == [api['id']]
    assert not data['issues']
    assert len(api['reads']) == 10
    assert {r['path'] for r in api['reads']} == {
        'Request[1].Locale', 'Request[1].HeaderName', 'Request[1].HeaderValue',
        'Request[1].ServiceUri', 'Request[1].AsAtDate', 'Request[1].RegionCode',
        'Request[1].IncludeArchived', 'Request[1].CollectionKey',
        'Context[1].Selection[1].PortfolioKey', 'Context[1].Selection[1].CustomerKey',
    }
    variants = api['api']['templates']['path']['variants']
    assert len(variants) == 3
    assert [[g['branch'] for g in v['guards']] for v in variants] == [[True], [False, True], [False, False]]
    assert variants[0]['template'].startswith('Portfolios(')
    assert variants[1]['template'].lstrip().startswith('Collections(')
    assert variants[2]['template'].lstrip().startswith('Customers(')
    assert all(v['template'].endswith('/recordTypes') for v in variants)
    # Query conditions control fragments, not whether the API rule runs.
    assert api['guards'] == []
    query = api['api']['templates']['query']
    assert query['status'] == 'parsed' and not query['variants']
    assert '.year()' in query['raw']
    archive = [d for d in query['dependencies'] if d['field'] == 'Request[1].IncludeArchived' and d['role'] == 'substitution']
    assert archive and archive[0]['guards'][0]['condition'] == 'Request[1].IncludeArchived != null'
    phase = next(n for n in data['nodes'].values() if n['tag'] == 'Phase')
    assert phase['attributes']['ProcessRulesOnly'] == 'Y'


def test_conditions_without_dollar_substitutions_are_data_dependencies(tmp_path):
    path = tmp_path / 'A.ifp'
    path.write_text('''<Phase Name="P">
      <Rule Name="control" RuleClassName="SetValueRule" PropertyName="Parameters[1].Switch" FromValue="Y"/>
      <Rule Name="read" RuleClassName="ReadRule" DatastoreGroup="Result" ResourcePath="$%IF Parameters[1].Switch != null$One$%ELSE$Two$%ENDIF$"/>
    </Phase>''')
    data = Trace(tmp_path, rules_config=PROFILE).collect(path.name, 'Result.Value', None, 'P')
    assert any(e['name'] == 'control' for e in data['events'])
    assert any(edge['field']['path'] == 'Parameters[1].Switch' for edge in data['edges'])


@pytest.mark.parametrize('text', ['$%IF P.X != null$A', '$%ELSE$A', '$%IF P.X != null$A$%ELSE$B$%ELSE$C$%ENDIF$', '$%WHILE X$A'])
def test_malformed_templates_never_export_plausible_partial_routes(text):
    result = template_evidence(text, variants=True)
    assert result['status'] == 'unresolved'
    assert result['raw'] == text
    assert not result['tree'] and not result['variants']


def test_variant_limit_preserves_tree_and_dependencies():
    text = ''.join(f'$%IF P.Flag{i} != null$A$%ELSE$B$%ENDIF$' for i in range(6))
    result = template_evidence(text, variants=True)
    assert result['status'] == 'limit' and result['reason'] == 'path_variant_limit'
    assert not result['variants']
    assert len(result['tree']) == 6
    assert {d['field'] for d in result['dependencies']} == {f'P.Flag{i}' for i in range(6)}


def test_rule_service_root_precedes_datasource_without_duplicating_endpoint(tmp_path):
    path = tmp_path / 'A.ifp'
    path.write_text('''<Project><DataSource Name="Shared" BaseURL="https://shared.invalid"/>
      <Phase Name="P">
        <Rule RuleClassName="ReadRule" Source="Shared" ServiceRootUri="https://inline.invalid" ResourcePath="Funds" DatastoreGroup="Result"/>
        <Rule RuleClassName="SwaggerIntegrationRule" Source="Shared" Endpoint="/funds" Output="Other"/>
      </Phase></Project>''')
    config = load_rule_config(PROFILE)
    operations = scan_integrator(path, config).operations
    assert [o['base_url'] for o in operations] == ['https://inline.invalid', 'https://shared.invalid']
    for field, expected in [('Result.Value', 'https://inline.invalid'), ('Other.Value', 'https://shared.invalid')]:
        data = Trace(tmp_path, rules_config=PROFILE).collect(path.name, field, None, 'P')
        api = next(e['api'] for e in data['events'] if e['kind'] == 'api' and e['id'] in data['seeds'])
        assert api['base_url'] == expected


def test_profile_is_extensible_and_bundle_replay_copies_it(tmp_path):
    path = fixture(tmp_path)
    text = path.read_text().replace('ReadRule', 'ProjectReadRule').replace('QueryOptions=', 'ProjectQuery=').replace('DatastoreGroup=', 'ProjectResult=')
    path.write_text(text)
    config = json.loads(PROFILE.read_text())
    config['api_rule_classes'] = ['ProjectReadRule']
    config['method_by_rule_class_suffix'] = {'ProjectReadRule': 'GET'}
    config['attribute_aliases'] = {'filter': ['ProjectQuery'], 'output': ['ProjectResult']}
    custom = tmp_path / 'config.json'
    custom.write_text(json.dumps(config))
    out = tmp_path / 'out'
    assert main(['trace', str(tmp_path), '--file', path.name, '--field', 'Response[1].Records[1].Name',
                 '--rules-config', str(custom), '--output', str(out)]) == 0
    custom.unlink()
    assert main(['trace', str(tmp_path), '--request', str(out / 'next.json'), '--output', str(tmp_path / 'again')]) == 0
    data = json.loads((tmp_path / 'again' / 'evidence.json').read_text())
    api = next(e['api'] for e in data['events'] if e['kind'] == 'api')
    assert api['method'] == 'GET' and len(api['templates']['path']['variants']) == 3


def test_unresolved_template_is_diagnosed_by_trace(tmp_path):
    path = tmp_path / 'A.ifp'
    path.write_text('<Phase Name="P"><Rule RuleClassName="ReadRule" DatastoreGroup="Result" ResourcePath="$%IF P.X != null$One"/></Phase>')
    data = Trace(tmp_path, rules_config=PROFILE).collect(path.name, 'Result.Value', None, 'P')
    assert any(i['code'] == 'API_TEMPLATE_UNRESOLVED' for i in data['issues'])
