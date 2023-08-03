#!/bin/sh
for pkg in *.pkg.tar.zst; do
  echo "Adding package $pkg to repository"
  repo-add --verify --sign $repo.db.tar.gz $pkg
done
