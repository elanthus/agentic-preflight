- A branch can no longer hide a submodule pointer change from review and risk
  classification by committing a `.gitmodules` file that sets `submodule.<name>.ignore = all`.
  Git read that file from the checkout and left the path out of the changed-file list and
  the review diff. The review now lists every submodule pointer change.
