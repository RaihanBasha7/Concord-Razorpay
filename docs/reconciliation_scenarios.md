# Reconciliation Scenarios — Concord Day 2 MVP

Hand-crafted acceptance scenarios for the deterministic reconciliation engine. Each scenario is concrete enough to later convert directly into pytest fixtures.

A valid reconciliation decision may involve two or more records and does not require representation from all three sources in every decision.

## 1. Unique Exact Identifier Match

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1001, order_id_hint=ORD-500, amount_paise=100000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2001, order_id_hint=ORD-500, amount_paise=100000, date=2026-08-26

**Configuration:** None required beyond defaults.

**Expected outcome:** Accepted as a reconciliation decision via EXACT_ID rule. Both records are resolved.

**Rationale:** The shared identifier ORD-500 is unambiguous within the reconciliation context. Exact identifier evidence is the strongest deterministic signal.

---

## 2. Ambiguous Exact Identifier

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1001, order_id_hint=ORD-500, amount_paise=100000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2001, order_id_hint=ORD-500, amount_paise=100000, date=2026-08-26
- Record C: source=BANK, source_native_id=BNK-2002, order_id_hint=ORD-500, amount_paise=100000, date=2026-08-27

**Configuration:** None required beyond defaults.

**Expected outcome:** No automatic reconciliation decision. All three records remain unresolved or are flagged as ambiguous.

**Rationale:** ORD-500 appears in three records, making the match ambiguous. The deterministic engine must not force-match duplicates or ambiguous candidates sharing the same identifier.

---

## 3. Unique Amount + Date Window Match

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1002, order_id_hint=None, amount_paise=50000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2003, order_id_hint=None, amount_paise=50000, date=2026-08-26

**Configuration:**
- amount_tolerance_paise=0
- date_window_days=2

**Expected outcome:** Accepted as a reconciliation decision via AMOUNT_AND_DATE rule. Both records are resolved.

**Rationale:** Amounts match exactly within tolerance, and the date difference is 1 day, which is within the configured window. No other candidate records share these attributes.

---

## 4. Ambiguous Same-Amount Candidates

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1003, order_id_hint=None, amount_paise=75000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2004, order_id_hint=None, amount_paise=75000, date=2026-08-25
- Record C: source=BANK, source_native_id=BNK-2005, order_id_hint=None, amount_paise=75000, date=2026-08-26

**Configuration:**
- amount_tolerance_paise=0
- date_window_days=2

**Expected outcome:** No automatic reconciliation decision. All three records remain unresolved.

**Rationale:** Multiple candidates match on amount and fall within the date window, making the match ambiguous. The deterministic engine must not force-match when unique candidates cannot be established.

---

## 5. Date Outside Allowed Window

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1004, order_id_hint=None, amount_paise=50000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2006, order_id_hint=None, amount_paise=50000, date=2026-08-29

**Configuration:**
- amount_tolerance_paise=0
- date_window_days=2

**Expected outcome:** No automatic reconciliation decision. Both records remain unresolved.

**Rationale:** Amounts match, but the date difference is 4 days, exceeding the configured window. Amount alone is never sufficient evidence.

---

## 6. Amount Difference Within Configured Tolerance

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1005, order_id_hint=None, amount_paise=100000, date=2026-08-25
- Record B: source=BANK, source_native_id=BNK-2007, order_id_hint=None, amount_paise=100050, date=2026-08-26

**Configuration:**
- amount_tolerance_paise=100
- date_window_days=2

**Expected outcome:** Accepted as a reconciliation decision via AMOUNT_AND_DATE rule. Both records are resolved.

**Rationale:** Amount difference is 50 paise, within the configured tolerance of 100 paise. Date difference is 1 day, within the window. No other candidates match these criteria.

---

## 7. Narration-Only Similarity

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1006, order_id_hint=None, amount_paise=200000, date=2026-08-25, narration="Payment for ORD-600"
- Record B: source=BANK, source_native_id=BNK-2008, order_id_hint=None, amount_paise=350000, date=2026-09-15, narration="ORD-600 transfer"

**Configuration:** None required beyond defaults.

**Expected outcome:** No automatic reconciliation decision. Both records remain unresolved.

**Rationale:** The only commonality is text similarity in narration fields. Amounts differ by 150000 paise and dates differ by 21 days, so the records cannot satisfy the Day 2 amount-plus-date deterministic matching rule under any reasonable configuration. Narration-only matches are ambiguous and non-deterministic, so they are deferred.

---

## 8. True Orphan

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1007, order_id_hint=None, amount_paise=150000, date=2026-08-25

**Configuration:** None required beyond defaults.

**Expected outcome:** No reconciliation decision. Record A remains unresolved as a true orphan.

**Rationale:** No counterpart record exists in the current dataset. True orphans have no deterministic evidence to support a match and must remain unresolved.

---

## 9. Duplicate Record Protection

**Records involved:**
- Record A: source=SETTLEMENT, source_native_id=SET-1008, order_id_hint=ORD-700, amount_paise=80000, date=2026-08-25
- Record B: source=SETTLEMENT, source_native_id=SET-1009, order_id_hint=ORD-700, amount_paise=80000, date=2026-08-25
- Record C: source=BANK, source_native_id=BNK-2009, order_id_hint=ORD-700, amount_paise=80000, date=2026-08-26

**Configuration:** None required beyond defaults.

**Expected outcome:** No automatic reconciliation decision. All three records remain unresolved or are flagged as duplicates.

**Rationale:** Multiple records share the same identifier. The deterministic engine must not allow duplicate records to participate in an automatic match. Exact identifier matches require unambiguous candidates. Duplicate handling must be explicit and conservative.
