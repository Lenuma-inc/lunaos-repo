#!/bin/sh
for pkg in .pkg.tar{.xz,.zst}; do
  echo "Adding package $pkg to repository"
  repo-add -n -R  lunaos-repo.db.tar.gz $pkg
  rm lunaos-repo.db
  rm lunaos-repo.files
  mv lunaos-repo.db.tar.gz lunaos-repo.db
  mv lunaos-repo.files.tar.gz lunaos-repo.files
done
