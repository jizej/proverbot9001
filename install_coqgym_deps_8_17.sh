#!/usr/bin/env bash

# Create the coq 8.12 switch
opam switch create coq-8.17 5.0.0
eval $(opam env --switch=coq-8.17 --set-switch)
opam pin add -y coq 8.17

# Install the packages that can be installed directly through opam
opam repo add coq-released https://coq.inria.fr/opam/released
opam repo add coq-extra-dev https://coq.inria.fr/opam/extra-dev
opam install -y coq-serapi \
     coq-smpl=8.17 coq-metacoq-template coq-metacoq-checker \
     coq-equations \
     coq-mathcomp-ssreflect coq-mathcomp-algebra coq-mathcomp-field \
     menhir

# Install some coqgym deps that don't have the right versions in their
# official opam packages
git clone git@github.com:uwplse/StructTact.git deps/StructTact
(cd deps/StructTact && opam install -y . )
git clone git@github.com:DistributedComponents/InfSeqExt.git deps/InfSeqExt
(cd deps/InfSeqExt && opam install -y . )
# Cheerios has its own issues
git clone git@github.com:uwplse/cheerios.git deps/cheerios
(cd deps/cheerios && opam install -y --ignore-constraints-on=coq . )
(cd coq-projects/verdi && opam install -y --ignore-constraints-on=coq . )
(cd coq-projects/fcsl-pcm && make "$@" && make install)

# Finally, sync the opam state back to global
rsync -av --delete /tmp/${USER}_dot_opam/ $HOME/.opam.dir | tqdm --desc="Writing shared opam state" > /dev/null
