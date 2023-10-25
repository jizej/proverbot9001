#!/usr/bin/env bash
# source swarm-prelude.sh

INIT_CMD=""

NTHREADS=1
while getopts ":j:" opt; do
  case "$opt" in
    j)
      NTHREADS="${OPTARG}"
      ;;
  esac
done

# Make sure ruby is in the path
export PATH=$HOME/.local/bin:$PATH

git submodule init && git submodule update

for project in $(jq -r '.[].project_name' coqgym_projs_splits.json); do
    SBATCH_FLAGS=""

    echo "#!/usr/bin/env bash" > coq-projects/$project/make.sh
    echo ${INIT_CMD} >> coq-projects/$project/make.sh
    echo $project
    echo "$PWD"
    if $(jq -e ".[] | select(.project_name == \"$project\") | has(\"build_command\")" \
         coqgym_projs_splits.json); then
        BUILD=$(jq -r ".[] | select(.project_name == \"$project\") | .build_command" \
                   coqgym_projs_splits.json)
    else
        BUILD="make"
    fi

    if $(jq -e ".[] | select(.project_name == \"$project\") | has(\"build_partition\")" \
            coqgym_projs_splits.json); then
        PART=$(jq -r ".[] | select(.project_name == \"$project\") | .build_partition" \
                   coqgym_projs_splits.json)
        SBATCH_FLAGS+=" -p $PART"
    fi

    if $(jq -e ".[] | select(.project_name == \"$project\") | has(\"timeout\")" \
         coqgym_projs_splits.json); then
        TIMEOUT=$(jq -r ".[] | select(.project_name == \"$project\") | .timeout" \
                     coqgym_projs_splits.json)
        SBATCH_FLAGS+=" --time=${TIMEOUT}"
    fi

    SWITCH=$(jq -r ".[] | select(.project_name == \"$project\") | .switch" coqgym_projs_splits.json)

    echo "eval \"$(opam env --set-switch --switch=coq-8.17)\"" >> coq-projects/$project/make.sh

    echo "$BUILD $@" >> coq-projects/$project/make.sh
    chmod u+x coq-projects/$project/make.sh
    (cd coq-projects/$project && ./make.sh)
done
