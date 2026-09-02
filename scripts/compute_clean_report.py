"""Compute metrics from clean Layer 2 evaluation results."""
import json, hashlib, sys, os
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

# Load .env
env_path = Path('.env')
if env_path.exists():
    with env_path.open('r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value

from reconciliation.audit import Auditor, make_audit_record
from reconciliation.domain.models import NormalizedRecord, SourceType
from reconciliation.evaluation.dataset_generator import generate_dataset, CATEGORY_QUOTAS
from reconciliation.evaluation.dataset_fingerprint import read_manifest
from reconciliation.evaluation.full_pipeline_evaluation import _build_ground_truth_units, _expected_match_ids
from reconciliation.evaluation.metrics import evaluate as evaluate_l1
from reconciliation.loader import load_residuals, load_normalized_records
from reconciliation.matcher import reconcile
from reconciliation.matcher_config import MatcherConfig
from reconciliation.normalizer import normalize_record
from reconciliation.proposal import MatchProposal
from reconciliation.proposal_validation import ProposalOutcome, ProposalOutcomeType
from reconciliation.layer3 import route as layer3_route
from reconciliation.routing import AUTO_ACCEPT_THRESHOLD, REVIEW_THRESHOLD

data_dir = Path('data')
manifest = read_manifest(data_dir)
fingerprint = manifest.fingerprint()
dataset = generate_dataset(seed=42)
config = MatcherConfig(amount_tolerance_paise=100, date_window_days=2)

# Load results
results_data = json.loads((data_dir / 'clean_eval_results.json').read_text())
raw_results = results_data['results']

# Load pipeline data
normalized = load_normalized_records(data_dir)
residuals = load_residuals(data_dir)
all_records = list(normalized)

scenario_map = {s.scenario_id: s for s in dataset.scenarios}
unit_map = {u.scenario_id: u for u in _build_ground_truth_units(list(dataset.scenarios))}
record_to_scenario = {}
scenario_to_records = {}
for unit in unit_map.values():
    for rid in unit.member_record_ids:
        record_to_scenario[rid] = unit.scenario_id
    scenario_to_records[unit.scenario_id] = list(unit.member_record_ids)

# Layer 1
l1_result = reconcile(all_records, config)
l1_report = evaluate_l1(
    scenarios=list(dataset.scenarios),
    decisions=list(l1_result.decisions),
    residual_record_ids=list(l1_result.residual_record_ids),
    scenario_units=list(unit_map.values()),
)
l1_eval_map = {ev.scenario_id: ev for ev in l1_report.scenario_evaluations}
l1_matched = len(l1_result.decisions)
l1_residual_records = len(l1_result.residual_record_ids)
l1_residual_scenarios = len(l1_report.pipeline_residual_scenario_ids)

# Map L2 results
l2_results_map = {r['scenario_id']: r for r in raw_results}

# Build L2 outcomes for Layer 3
def build_outcome(r):
    sid = r['scenario_id']
    residual = next((x for x in residuals if x.scenario_id == sid), None)
    if residual is None:
        return None
    member_ids = list(residual.member_record_ids)
    if r['outcome'] == 'PROPOSAL_VALID' and r['confidence'] is not None:
        proposal = MatchProposal(proposed_match_ids=member_ids, confidence=r['confidence'], rationale='Clean eval')
        ot = ProposalOutcomeType.PROPOSAL_VALID
    elif r['outcome'] == 'NO_PROPOSAL':
        proposal = None; ot = ProposalOutcomeType.NO_PROPOSAL
    elif r['outcome'] == 'VALIDATION_FAILED':
        proposal = None; ot = ProposalOutcomeType.VALIDATION_FAILED
    else:
        proposal = None; ot = ProposalOutcomeType.API_ERROR
    return ProposalOutcome(outcome=ot, proposal=proposal, presented_record_ids=tuple(member_ids),
                           reason=r.get('diagnostic', '') or '', diagnostic=r.get('diagnostic'),
                           error_classification=r.get('error_classification'))

l2_outcomes = [oc for r in raw_results if (oc := build_outcome(r)) is not None]
for r in residuals:
    if r.scenario_id not in l2_results_map:
        l2_outcomes.append(ProposalOutcome(
            outcome=ProposalOutcomeType.API_ERROR, proposal=None,
            presented_record_ids=tuple(r.member_record_ids),
            reason='Not attempted (quota exhausted)', diagnostic='quota_exhausted',
            error_classification='quota_exhausted'))

# Layer 3
routing_decisions = layer3_route(
    layer1_decisions=list(l1_result.decisions),
    layer2_outcomes=l2_outcomes,
    all_records=all_records,
)
bucket_counts = Counter(rd.bucket.value for rd in routing_decisions)
record_routing = {rd.record_id: rd for rd in routing_decisions}

# Edge-case breakdown
cat_stats = defaultdict(lambda: {'count': 0, 'l1_matched': 0, 'l2_attempted': 0, 'l2_successful': 0,
                                  'l2_proposals_valid': 0, 'auto_accept': 0, 'review': 0, 'exception': 0,
                                  'has_real_match': 0})
for scen in dataset.scenarios:
    sid = scen.scenario_id
    cat = scen.category.value
    unit = unit_map[sid]
    cs = cat_stats[cat]
    cs['count'] += 1
    if scen.has_real_match:
        cs['has_real_match'] += 1
    l1_eval = l1_eval_map.get(sid)
    if l1_eval and l1_eval.matched:
        cs['l1_matched'] += 1
    if sid in l2_results_map:
        cs['l2_attempted'] += 1
        l2r = l2_results_map[sid]
        if l2r['outcome'] in ('PROPOSAL_VALID', 'NO_PROPOSAL', 'VALIDATION_FAILED'):
            cs['l2_successful'] += 1
        if l2r['outcome'] == 'PROPOSAL_VALID':
            cs['l2_proposals_valid'] += 1
    for rid in unit.member_record_ids:
        rd = record_routing.get(rid)
        if rd:
            if rd.bucket.value == 'AI_AUTO_ACCEPTED':
                cs['auto_accept'] += 1
            elif rd.bucket.value == 'HUMAN_REVIEW':
                cs['review'] += 1
            elif rd.bucket.value == 'EXCEPTION':
                cs['exception'] += 1
            break

# False accepts
false_accept_scenarios = []
for rd in routing_decisions:
    if rd.bucket.value == 'AI_AUTO_ACCEPTED':
        scen_id = record_to_scenario.get(rd.record_id)
        if scen_id and scen_id in scenario_map:
            scen = scenario_map[scen_id]
            unit = unit_map[scen_id]
            expected = tuple(sorted(_expected_match_ids(scen, unit)))
            l2r = l2_results_map.get(scen_id)
            if l2r and l2r['confidence'] is not None:
                proposed = tuple(sorted(scen.record_specs[i].source_type.value + '-' + hashlib.sha256(
                    f"{scen.record_specs[i].source_type.value}-{scen.record_specs[i].source_native_id}-"
                    f"{scen.record_specs[i].order_id_hint or 'NA'}-{scen.record_specs[i].amount_paise}-"
                    f"{scen.record_specs[i].record_date.isoformat()}".encode()
                ).hexdigest()[:12]) for i in range(len(scen.record_specs)))
                # Use ground truth for correctness
                if proposed != expected and not unit.is_true_orphan:
                    false_accept_scenarios.append({
                        'scenario_id': scen_id, 'category': cat,
                        'confidence': l2r['confidence'],
                    })
            break

# Stats
stats = {
    'attempted': len(raw_results),
    'successful': sum(1 for r in raw_results if r['outcome'] in ('PROPOSAL_VALID', 'NO_PROPOSAL', 'VALIDATION_FAILED')),
    'api_errors': sum(1 for r in raw_results if r['outcome'] == 'API_ERROR'),
    'quota_exhausted': sum(1 for r in raw_results if r.get('error_classification') == 'quota_exhausted'),
    'transient_errors': sum(1 for r in raw_results if r.get('error_classification') == 'transient'),
    'valid_proposals': sum(1 for r in raw_results if r['outcome'] == 'PROPOSAL_VALID'),
    'no_proposals': sum(1 for r in raw_results if r['outcome'] == 'NO_PROPOSAL'),
    'validation_failed': sum(1 for r in raw_results if r['outcome'] == 'VALIDATION_FAILED'),
    'unevaluated': len(residuals) - len(raw_results),
}

# Confidence distribution
conf_dist = {'>=0.90': 0, '0.75-0.89': 0, '0.60-0.74': 0, '<0.60': 0}
for r in raw_results:
    c = r.get('confidence')
    if c is not None and r['outcome'] == 'PROPOSAL_VALID':
        if c >= 0.90: conf_dist['>=0.90'] += 1
        elif c >= 0.75: conf_dist['0.75-0.89'] += 1
        elif c >= 0.60: conf_dist['0.60-0.74'] += 1
        else: conf_dist['<0.60'] += 1

# Create canonical artifact
artifact_path = data_dir / 'layer2_clean_audit.jsonl'
auditor = Auditor(artifact_path, archive_existing=False, atomic_write=True)
for r in raw_results:
    sid = r['scenario_id']
    residual = next((x for x in residuals if x.scenario_id == sid), None)
    if residual is None: continue
    member_ids = list(residual.member_record_ids)
    proposal = None
    if r['outcome'] == 'PROPOSAL_VALID' and r['confidence'] is not None:
        proposal = MatchProposal(proposed_match_ids=member_ids, confidence=r['confidence'], rationale='Clean eval')
    auditor.write(make_audit_record(
        correlation_id=f'{sid}-{fingerprint[:8]}',
        presented_record_ids=member_ids, outcome=r['outcome'], proposal=proposal,
        reason=r.get('diagnostic', '') or '', dataset_fingerprint=fingerprint,
        diagnostic=r.get('diagnostic')))
auditor.finalize()
artifact_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()

# Save manifest
manifest_dict = manifest.to_dict()
manifest_dict['canonical_layer2_artifact'] = {
    'filename': artifact_path.name,
    'sha256': artifact_hash,
    'generation_timestamp': datetime.now(timezone.utc).isoformat(),
    'record_count': auditor.write_count,
    'note': f'Fresh clean evaluation. Dataset fingerprint: {fingerprint}. Partial: {stats["unevaluated"]} unevaluated due to Groq quota.',
}
(data_dir / 'dataset_manifest.json').write_text(json.dumps(manifest_dict, indent=2), encoding='utf-8')

# === PRINT RESULTS ===
print(f'ARTIFACT: {artifact_path.name}')
print(f'ARTIFACT_SHA256: {artifact_hash}')
print(f'RECORDS: {auditor.write_count}')
print(f'FINGERPRINT: {fingerprint}')
print()
print(f'EVALUATION_TYPE: PARTIAL')
print(f'ATTEMPTED: {stats["attempted"]}')
print(f'SUCCESSFUL: {stats["successful"]}')
print(f'API_ERRORS: {stats["api_errors"]}')
print(f'  QUOTA_EXHAUSTED: {stats["quota_exhausted"]}')
print(f'  TRANSIENT: {stats["transient_errors"]}')
print(f'VALID_PROPOSALS: {stats["valid_proposals"]}')
print(f'NO_PROPOSALS: {stats["no_proposals"]}')
print(f'VALIDATION_FAILED: {stats["validation_failed"]}')
print(f'UNEVALUATED: {stats["unevaluated"]}')
print()
print(f'LAYER1_MATCHED: {l1_matched}/{len(all_records)} ({l1_matched/len(all_records):.1%})')
print(f'LAYER1_RESIDUAL_RECORDS: {l1_residual_records}')
print(f'LAYER1_RESIDUAL_SCENARIOS: {l1_residual_scenarios}')
print()
print(f'LAYER3_ROUTING: {dict(bucket_counts)}')
print(f'FALSE_ACCEPTS: {len(false_accept_scenarios)}')
print()
print(f'CONFIDENCE_DISTRIBUTION: {conf_dist}')
print()
print(f'EDGE_CASE_BREAKDOWN:')
for cat in sorted(cat_stats.keys()):
    cs = cat_stats[cat]
    print(f'  {cat:30s} n={cs["count"]:2d} L1={cs["l1_matched"]:2d} L2_att={cs["l2_attempted"]:2d} L2_ok={cs["l2_successful"]:2d} auto={cs["auto_accept"]:2d} rev={cs["review"]:2d} exc={cs["exception"]:2d}')
