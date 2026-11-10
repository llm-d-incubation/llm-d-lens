"""Argo CD as a delivery backend (``--backend argocd``).

With the direct backend llmd applies a stack's rendered objects itself. With Argo CD, llmd still
renders and checks the stack the same way, but what it submits is one Argo CD Application that
points at the stack's own files in Git; Argo CD renders them with its own tools and applies them.

* :mod:`.application` builds the Application from a plan: its sources, and the rest from a
  template the team keeps;
* :mod:`.origin` finds the repository URL and the revision Argo CD fetches, and says when the
  checkout holds what Argo CD would not see.

Nothing here names a directory of a source: every path comes from the stack's plan.
"""
