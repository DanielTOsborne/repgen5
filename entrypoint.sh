#!/bin/sh

input="-"
if [ ! -z "${INPUT_FILE}" ]; then
	scheme="$(echo "${INPUT_FILE}" | cut -d: -f1)"
	if [ "${scheme}" != "s3" ]; then
		usage "BATCH_FILE_S3_URL must be for an S3 object; expecting URL starting with s3://"
	fi

	# mktemp arguments are not very portable.  We make a temporary directory with
	# portable arguments, then use a consistent filename within.
	TMPDIR="$(mktemp -d -t tmp.XXXXXXXXX)" || error_exit "Failed to create temp directory."
	TMPFILE="${TMPDIR}/input-file-temp"
	OUTPUT="${TMPDIR}/output-file-temp"
	install -m 0600 /dev/null "${TMPFILE}" || error_exit "Failed to create temp file."

	aws s3 cp "${INPUT_FILE}" - > "${TMPFILE}" || error_exit "Failed to download S3 script."
	input="${TMPFILE}"
fi

if [ "$1" = "convert" ]; then
	shift
	exec /converter/convert_report.py "$@"
elif [ "$1" = "batch_convert" ]; then
	shift
	exec /converter/convert.sh "$@"
else
	# Check if -i was supplied, as we don't want to override it
	# If not present, set it to stdin
	found=0
	for arg do
		case $arg in
			-i)	found=1	;;
			-i*)	found=1	;;
			--in)	found=1	;;
			--in=*)	found=1	;;
		esac
	done

	args=""
	[ -n "$CDA_BACKUP" ] && args="$args --alternate $CDA_BACKUP"
	[ -n "$COMPATIBILITY_MODE" ] && [ $COMPATIBILITY_MODE -gt 0 ] && args="$args --compatibility"
	[ -n "$CDA_TIMEOUT" ] && args="$args --timeout $CDA_TIMEOUT"
	[ -n "$OFFICE_ID" ] && args="$args --office $OFFICE_ID"
	[ -n "$DATE" ] && args="$args --date $DATE"
	[ -n "$TIME" ] && args="$args --time $TIME"
	[ -n "$DATA_FILE" ] && args="$args --file \"$DATA_FILE\""
	# TZ is already handled directly by repgen

	[ $found -eq 0 ] && [ -z $input ] && args="$args -i-"
	[ ! -z $input ] && args="$args -i$input"

	# If S3 path specified for output, use temp path
	[ ! -z $OUTPUT_FILE ] && args="$args -i$OUTPUT"

	args="--address $CDA_PRIMARY $args"
	[ -n "$VERBOSE" ] && [ $VERBOSE -gt 0 ] && set>&2 &&
		echo /repgen $args "$@">&2
	/repgen $args "$@"

	[ ! -z $OUTPUT_FILE ] && aws s3 cp ${OUTPUT} ${OUTPUT_FILE}
fi
