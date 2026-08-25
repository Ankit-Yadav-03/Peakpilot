# Deployment Reality

## Purpose

This document captures the current operational reality of the system, validated capabilities, known limitations, deployment assumptions, and deployment procedures.

This document is the source of truth for:

* Pilot offer design
* Customer qualification
* Sales conversations
* Deployment planning
* Future roadmap decisions

If any marketing, sales, or product claim conflicts with this document, this document takes precedence.

---

# Validation Levels

## Simulation Validated

Capability has been validated through end-to-end system simulation.

---

## Pilot Validated

Capability has been validated in a customer facility pilot deployment.

---

## Production Validated

Capability has been validated across multiple customer deployments.

---

## Current Validation Status

Current system status:

* Simulation Validated
* Pilot Validation Pending
* Production Validation Pending

No live customer deployment has yet been completed.

---

# Current Product State

The system is a real-time demand management and operational recommendation platform.

The platform continuously:

1. Ingests meter telemetry
2. Detects peak-demand risk
3. Generates operational recommendations
4. Tracks execution outcomes
5. Measures operational compliance
6. Estimates projected savings

The platform does not currently perform causal savings attribution.

---

# Proven Capabilities

## Real-Time Demand Monitoring

The platform continuously monitors:

* kW
* kVA
* Power factor
* Demand trends
* MDI risk

Status:

Simulation Validated

---

## Peak Risk Detection

The platform identifies periods where facility demand is approaching:

* Contract demand
* MDI thresholds
* Demand-charge exposure

Status:

Simulation Validated

---

## Automated Load Recommendations

The platform generates:

* SHED recommendations
* RESTORE recommendations

Outputs include:

* Loads selected
* Expected demand reduction
* Projected savings
* Recommendation rationale

Status:

Simulation Validated

---

## Per-Load Execution Tracking

The platform creates execution records for each recommended load.

Tracked fields include:

* Load ID
* Command type
* Execution status
* Confirmation source
* Confirmation latency
* Expected reduction
* Measured reduction

Status:

Simulation Validated

---

## Compliance Measurement

The platform measures:

* Total loads targeted
* Loads followed
* Loads ignored
* Compliance percentage

Compliance is calculated using per-load execution outcomes.

Status:

Simulation Validated

---

## Operational Visibility

The platform can determine:

* Which recommendations were issued
* Which loads were targeted
* Which loads responded
* Which loads ignored recommendations
* Confirmation source
* Execution latency

Status:

Simulation Validated

---

# Training Data Generation

The platform records:

* Recommendation intent
* Per-load execution outcomes
* Compliance percentages
* Confirmation sources
* Response latency

This enables future learning systems to train on actual execution behavior rather than recommendation issuance alone.

Current training-data generation status:

Simulation Validated

---

# Current Economic Capability

## Projected Savings

The platform estimates:

Projected Demand Charge Avoidance

based on:

* Expected demand reduction
* Tariff configuration
* Peak-avoidance logic

Status:

Simulation Validated

---

## Realized Savings

The platform does not currently calculate:

* Verified savings
* Causal savings attribution
* Utility bill reconciliation
* Financial settlement value

Status:

Not Implemented

Any savings value shown by the system should be treated as projected savings only.

---

# Execution Confirmation Model

Execution confirmation can occur through multiple evidence sources.

---

## Equipment Confirmation

Evidence:

* Equipment state changed as expected

Sources:

* EQUIPMENT
* EQUIPMENT_AND_METER

Confidence:

High

---

## Meter Confirmation

Evidence:

* Aggregate facility demand changed as expected

Source:

* METER

Confidence:

Moderate

Meter confirmation proves facility-level response.

Meter confirmation does not necessarily prove that a specific load changed state.

---

# Current Compliance Definition

Compliance is calculated using per-load execution records.

A load is considered followed only when:

* Execution status is confirmed
* Confirmation includes equipment evidence

Accepted confirmation sources:

* EQUIPMENT
* EQUIPMENT_AND_METER

Meter-only confirmations are recorded and retained for analysis but are excluded from compliance calculations.

This distinction exists to preserve training-data quality.

---

# What The Platform Does Not Require

Initial pilots do not require:

* New electrical panels
* Load controllers
* Hardware retrofits
* Equipment replacement
* Utility approval
* Automatic equipment control

Pilots can operate using existing telemetry and operator actions.

---

# Known Limitations

## No Direct Equipment Control

Current deployment assumes recommendations are executed externally.

Possible actors include:

* Operators
* Existing BMS
* Existing PLC
* Existing SCADA

The platform currently observes outcomes.

The platform does not directly control equipment.

---

## No Verified Financial Attribution

Current system measures:

Projected value

not

Verified financial outcome.

---

## Meter-Based Ambiguity

Facility-level demand changes can occur even when a specific load does not respond.

This can produce:

* Meter-confirmed execution
* Equipment-nonconfirmed execution

Current compliance reporting accounts for this distinction.

---

## Single-Facility Validation Only

Current validation has been performed using:

* Simulated environments
* Single-facility architecture

Live facility validation remains pending.

Multi-site validation remains pending.

---

# Deployment Scenarios

## Scenario A - Meter Only Facility

Available:

* Demand monitoring
* Peak-risk detection
* Recommendations
* Projected savings

Unavailable:

* Per-load compliance tracking
* Equipment confirmation

Recommended Pilot:

Observation Pilot

Confidence:

Medium

---

## Scenario B - Meter + Equipment Telemetry

Available:

* Demand monitoring
* Peak-risk detection
* Recommendations
* Execution tracking
* Compliance tracking
* Projected savings

Recommended Pilot:

Full Visibility Pilot

Confidence:

High

---

## Scenario C - Meter + BMS Integration

Available:

* Demand monitoring
* Recommendations
* Execution tracking
* Compliance tracking
* Projected savings

Potentially Available:

* Automated dispatch through customer BMS integration
* Automated dispatch through PLC integration
* Closed-loop automation

Field validation pending.

Recommended Pilot:

Integrated Visibility Pilot

Confidence:

Medium

Integration effort required.

---

## Scenario D - Multi-Facility Deployment

Available:

* Facility-level optimization
* Fleet-level visibility

Not Yet Proven:

* Portfolio optimization
* Cross-site orchestration
* Fleet-wide recommendation coordination

Recommended Pilot:

Phased Rollout

---

# Pilot Objective

The primary objective of the pilot is not energy optimization.

The primary objective is to determine:

* Whether avoidable demand-charge exposure exists
* Whether demand-risk events can be detected before they occur
* Whether operators can act on recommendations in time
* Whether measurable operational compliance exists

---

# Pilot Deployment Procedure

## Phase 1

Discovery

Collect:

* Electricity bill
* Tariff information
* Contract demand
* Operating schedule
* High-level load inventory

---

## Phase 2

Telemetry Integration

Connect:

* Meter telemetry
* Equipment telemetry (if available)

Validate:

* Data quality
* Data freshness
* Timestamp consistency

---

## Phase 3

Observation Mode

Run system without operational intervention.

Validate:

* Peak-risk detection
* Demand forecasting behavior
* Recommendation quality

Duration:

1–2 weeks

---

## Phase 4

Recommendation Mode

Begin operational response to recommendations.

Track:

* Compliance
* Response latency
* Peak-risk events
* Projected savings

Duration:

2–4 weeks

---

## Phase 5

Pilot Review

Deliver:

* Peak-risk events detected
* Recommendations issued
* Compliance percentage
* Operational findings
* Projected savings opportunity

---

# Roadmap Items

Future improvements include:

* Realized savings attribution
* Equipment control integration
* Portfolio optimization
* Automated dispatch
* Compliance prediction
* Recommendation acceptance prediction
* Reinforcement learning from execution outcomes

---

# Current Positioning

The platform should currently be positioned as:

A real-time demand-risk detection, operational recommendation, and compliance visibility platform.

The platform helps facilities identify avoidable demand-charge exposure before utility bills are generated.

---

# Positioning To Avoid

The platform should not currently be positioned as:

* A verified savings measurement platform
* A utility-bill auditing platform
* A fully autonomous load-control platform
* A proven multi-site optimization platform
* A realized savings attribution platform
