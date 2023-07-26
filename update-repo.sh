#!/bin/sh
for pkg in *.pkg.tar.zst; do
  echo "Adding package $pkg to repository"
  repo-add -n -R $repo.db.tar.gz $pkg
  rm $repo.db
  rm $repo.files
  mv $repo.db.tar.gz $repo.db
  mv $repo.files.tar.gz $repo.files
done
