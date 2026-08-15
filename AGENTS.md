# Documentation Rules

- Do not modify the README.md file in the project root directory unless explicitly requested.

# Bug Fixing Principles

- When a bug is discovered, do not immediately apply a temporary patch.
- First analyze the root cause of the issue.
- Consider the problem from a system-level perspective:
  - Is the current architecture causing this issue?
  - Is there a missing abstraction or incorrect responsibility separation?
  - Does another part of the system need optimization or redesign?
- Prefer fundamental fixes that improve system reliability and maintainability over short-term workarounds.
- Avoid introducing special cases or hacks unless there is a clear justification.

# Compatibility Principles

- Backward compatibility is not required.
- When making changes, prioritize correctness, maintainability, and architectural consistency over preserving legacy behavior.
- Breaking changes are acceptable when they improve the system design or resolve fundamental issues.

# Before Making Changes

Before modifying code:

1. Understand the existing architecture.
2. Identify the affected components.
3. Explain the root cause of the problem.
4. Consider whether the issue indicates a broader design problem.
5. Only then implement the fix.