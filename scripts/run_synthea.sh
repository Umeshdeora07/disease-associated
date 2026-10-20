#!/usr/bin/env bash
# Generate the Synthea cohorts behind the GE-EHR evaluation (SPEC §10, paper §5).
#
#   scripts/run_synthea.sh [correctness|scale|all]        (default: all)
#
# Two cohorts are generated with fixed seeds and a fixed reference and end date (Synthea's end
# date otherwise defaults to the current day), so a re-run with the same Synthea build
# reproduces them exactly:
#
#   correctness_30   30 living patients,      seed 30
#   scale_10000      10,000 living patients,  seed 10000
#
# Only the CSV exporter runs, restricted to the three files GE-EHR reads (patients.csv,
# conditions.csv, medications.csv), with the full history kept (exporter.years_of_history=0) so
# the earliest diabetes diagnosis and every medication start/stop survive the export.
# generate.only_alive_patients=true makes the population counts exact (deceased patients are
# re-drawn). Every Synthea module runs (diabetes comes from metabolic_syndrome_disease /
# metabolic_syndrome_care and their medication submodule): the full 10,000-patient run takes
# about 13 minutes on 4 cores, so no module filter is needed. Set SYNTHEA_MODULES to a
# path-separator list of module filters (Synthea's undocumented -m option, e.g.
# "metabolic_syndrome*") to restrict the disease modules — Synthea's core Java modules
# (lifecycle, encounters, insurance, ...) always run.
#
# Environment:
#   SYNTHEA_DIR      work directory for the jar and raw output (default: <repo>/synthea, git-ignored)
#   SYNTHEA_JAR      jar path (default: $SYNTHEA_DIR/synthea-with-dependencies.jar; downloaded
#                    from SYNTHEA_JAR_URL when missing)
#   SYNTHEA_JAR_URL  default: the "master-branch-latest" GitHub release asset
#   SYNTHEA_MODULES  optional module filter passed as -m (default: all modules)
#   JAVA             java executable (default: java; Java 17+ required)
#   JAVA_OPTS        JVM options (default: -Xmx6g)
#
# Output per cohort in $SYNTHEA_DIR/output/<cohort>/: csv/{patients,conditions,medications}.csv,
# Synthea's run metadata in metadata/, and run_info.txt (key=value lines: the jar URL and
# SHA-256 and the exact Synthea arguments); the console log goes to $SYNTHEA_DIR/output/<cohort>.log.
# Convert to the committed compact cohorts with:
#
#   .venv/bin/python scripts/build_cohorts.py --correctness-dir "$SYNTHEA_DIR/output/correctness_30" \
#       --scale-dir "$SYNTHEA_DIR/output/scale_10000"
#
# All output is synthetic (Synthea); no real patient data is involved.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SYNTHEA_DIR="${SYNTHEA_DIR:-$REPO_ROOT/synthea}"
SYNTHEA_JAR="${SYNTHEA_JAR:-$SYNTHEA_DIR/synthea-with-dependencies.jar}"
SYNTHEA_JAR_URL="${SYNTHEA_JAR_URL:-https://github.com/synthetichealth/synthea/releases/download/master-branch-latest/synthea-with-dependencies.jar}"
SYNTHEA_MODULES="${SYNTHEA_MODULES:-}"
JAVA="${JAVA:-java}"
JAVA_OPTS="${JAVA_OPTS:--Xmx6g}"

# Reference date (ages) and simulation end date ("today" inside Synthea), fixed for reproducibility.
REFERENCE_DATE="20261001"
STATE="Massachusetts"
CORRECTNESS_POPULATION=30
CORRECTNESS_SEED=30
SCALE_POPULATION=10000
SCALE_SEED=10000

# usage EXIT_STATUS
usage() {
    sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
    exit "$1"
}

ensure_jar() {
    if [[ -f "$SYNTHEA_JAR" ]]; then
        return
    fi
    mkdir -p "$(dirname "$SYNTHEA_JAR")"
    echo "Downloading Synthea from $SYNTHEA_JAR_URL" >&2
    curl --fail --location --silent --show-error --output "$SYNTHEA_JAR.part" "$SYNTHEA_JAR_URL"
    mv "$SYNTHEA_JAR.part" "$SYNTHEA_JAR"
}

# run_cohort NAME POPULATION SEED
run_cohort() {
    local name="$1" population="$2" seed="$3"
    local out="$SYNTHEA_DIR/output/$name"
    local synthea_args=(-s "$seed" -cs "$seed" -r "$REFERENCE_DATE" -e "$REFERENCE_DATE" -p "$population")
    if [[ -n "$SYNTHEA_MODULES" ]]; then
        synthea_args+=(-m "$SYNTHEA_MODULES")
    fi
    synthea_args+=(
        --exporter.csv.export=true
        --exporter.csv.included_files=patients.csv,conditions.csv,medications.csv
        --exporter.years_of_history=0
        --exporter.fhir.export=false
        --exporter.hospital.fhir.export=false
        --exporter.practitioner.fhir.export=false
        --exporter.metadata.export=true
        --generate.only_alive_patients=true
        --generate.log_patients.detail=simple
        "$STATE"
    )
    rm -rf "$out"
    mkdir -p "$out"
    {
        echo "jar_url=$SYNTHEA_JAR_URL"
        echo "jar_sha256=$(sha256sum "$SYNTHEA_JAR" | cut -d' ' -f1)"
        echo "synthea_args=${synthea_args[*]}"
    } > "$out/run_info.txt"
    echo "Generating $name: $population patients, seed $seed, reference date $REFERENCE_DATE -> $out" >&2
    # shellcheck disable=SC2086  # JAVA_OPTS is a list of JVM options
    if ! "$JAVA" $JAVA_OPTS -jar "$SYNTHEA_JAR" --exporter.baseDirectory="$out/" "${synthea_args[@]}" > "$out.log" 2>&1; then
        echo "Synthea failed for $name; see $out.log" >&2
        exit 1
    fi
    echo "Finished $name; log: $out.log" >&2
}

main() {
    local target="${1:-all}"
    case "$target" in
        correctness | scale | all) ;;
        -h | --help) usage 0 ;;
        *) echo "Unknown cohort '$target' (expected correctness, scale or all)." >&2; usage 2 ;;
    esac
    ensure_jar
    if [[ "$target" == correctness || "$target" == all ]]; then
        run_cohort "correctness_$CORRECTNESS_POPULATION" "$CORRECTNESS_POPULATION" "$CORRECTNESS_SEED"
    fi
    if [[ "$target" == scale || "$target" == all ]]; then
        run_cohort "scale_$SCALE_POPULATION" "$SCALE_POPULATION" "$SCALE_SEED"
    fi
}

main "$@"
