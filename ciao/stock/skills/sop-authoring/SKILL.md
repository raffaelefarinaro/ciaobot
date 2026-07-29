---
name: sop-authoring
description: Guide for authoring clean, modular agent SOPs (Standard Operating Procedures) and skills according to AI OS context principles.
---

# SOP & Skill Authoring Guide

Use this skill when drafting new agent SOPs, custom skills, or operational instructions for Ciaobot background routines.

## Core AI OS Principles

1. **Partition Expertise vs. Situational Context**:
   - **System Expertise (SOP)**: Fixed domain rules, step-by-step procedures, required parameters, and failure recovery. Keep static and clean.
   - **Situational Context**: Fleeting parameters passed per turn (e.g. today's date, current project, active workspace). Never embed transient session details into the permanent SOP.

2. **Skill Budgeting & Size Limits**:
   - Keep `SKILL.md` under **15,000 bytes** to prevent context bloat and model degradation.
   - If instructions exceed budget, split reference data into a `references/` or `examples/` subfolder.

3. **Frontmatter Specification**:
   ```yaml
   ---
   name: skill-name-kebab-case
   description: Actionable, concise summary of what this skill does and when to invoke it.
   ---
   ```

4. **SOP Structure Template**:
   - **Goal & Overview**: What the SOP accomplishes.
   - **Prerequisites & Tools**: Required commands or APIs.
   - **Execution Workflow**: Step 1, Step 2, Step 3.
   - **Error Handling & Fallbacks**: What to do when an API or command fails.
   - **Verification**: How to confirm success.
