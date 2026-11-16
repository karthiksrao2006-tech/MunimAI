#!/usr/bin/env python
# -*- coding: utf-8 -*-
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, KeepTogether, ListFlowable, ListItem
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER

INK = colors.HexColor("#1A1D16")
INK2 = colors.HexColor("#5B5F52")
ACCENT = colors.HexColor("#C65D2E")
GOOD = colors.HexColor("#2E6E5E")
WARN = colors.HexColor("#B8862E")
LINE = colors.HexColor("#D8D4C6")
PANEL = colors.HexColor("#F6F4EE")

styles = getSampleStyleSheet()
styles.add(ParagraphStyle("H1", fontName="Helvetica-Bold", fontSize=22, leading=26,
                           textColor=INK, spaceAfter=4))
styles.add(ParagraphStyle("Sub", fontName="Helvetica", fontSize=10.5, leading=15,
                           textColor=INK2, spaceAfter=16))
styles.add(ParagraphStyle("H2", fontName="Helvetica-Bold", fontSize=14, leading=18,
                           textColor=INK, spaceBefore=18, spaceAfter=8))
styles.add(ParagraphStyle("Body", fontName="Helvetica", fontSize=10, leading=15,
                           textColor=INK, spaceAfter=6, alignment=TA_LEFT))
styles.add(ParagraphStyle("BodySmall", fontName="Helvetica", fontSize=9, leading=13,
                           textColor=INK2, spaceAfter=4))
styles.add(ParagraphStyle("Eyebrow", fontName="Helvetica-Bold", fontSize=9, leading=12,
                           textColor=ACCENT, spaceAfter=2))
styles.add(ParagraphStyle("TableHead", fontName="Helvetica-Bold", fontSize=9,
                           textColor=INK2, leading=12))
styles.add(ParagraphStyle("TableCell", fontName="Helvetica", fontSize=9.5,
                           textColor=INK, leading=13))
styles.add(ParagraphStyle("TableCellBold", fontName="Helvetica-Bold", fontSize=9.5,
                           textColor=INK, leading=13))
styles.add(ParagraphStyle("Footer", fontName="Helvetica", fontSize=8,
                           textColor=INK2, leading=11))

doc = SimpleDocTemplate(
    "MunimAI_Build_Plan.pdf", pagesize=A4,
    leftMargin=22*mm, rightMargin=22*mm, topMargin=20*mm, bottomMargin=20*mm,
    title="MunimAI Build Plan", author="MunimAI team"
)

story = []

def hr():
    story.append(HRFlowable(width="100%", thickness=0.75, color=LINE, spaceBefore=4, spaceAfter=14))

def status_pill(text, color):
    return Paragraph(f'<font color="{color.hexval() if hasattr(color,"hexval") else color}">{text}</font>', styles["TableCellBold"])

# ---------- COVER / HEADER ----------
story.append(Paragraph("Tech4Bharat 2026 &middot; PS 3.2 (3.1 inside) &middot; Agentic AI", styles["Eyebrow"]))
story.append(Paragraph("MunimAI &mdash; Build Plan", styles["H1"]))
story.append(Paragraph(
    "What is being built, on which hardware, in what order, and exactly what has been "
    "installed so far on the college supercomputer (PS-BOSS-OS). This is the plan reviewed "
    "before each build step proceeds.", styles["Sub"]))
hr()

# ---------- 1. WHAT WE ARE BUILDING ----------
story.append(Paragraph("1. What we are building", styles["H2"]))
story.append(Paragraph(
    "<b>MunimAI</b>: an agentic assistant on WhatsApp, voice-first in Kannada/Hindi/English, "
    "built for micro-enterprise owners. Primary job (PS 3.2): match the business to schemes "
    "(MUDRA, PMEGP, Stand-Up India, CGTMSE) and assemble a lender-ready, consent-based credit "
    "file. Secondary job (PS 3.1, built as a feature inside it): turn daily bill photos and "
    "UPI activity into GST drafts, ITC-mismatch alerts and e-way bills &mdash; because that "
    "same compliance data is what proves the business is bankable.", styles["Body"]))
story.append(Paragraph(
    "Core thesis: compliance is not a cost centre, it is the data engine that makes a micro "
    "business visible to formal credit.", styles["Body"]))

# ---------- 2. AGENTIC ARCHITECTURE ----------
story.append(Paragraph("2. Agentic architecture", styles["H2"]))
story.append(Paragraph(
    "One orchestrator plans multi-day work and holds memory; six specialist agents do the work; "
    "one verifier blocks anything uncited, unconsented, or built on a stale rule version.", styles["Body"]))

arch_rows = [
    [Paragraph("Component", styles["TableHead"]), Paragraph("Role", styles["TableHead"])],
    [Paragraph("Tiered Cognition Router", styles["TableCellBold"]),
     Paragraph("Triages every message; ~85% answered by the small local model, hard cases escalate to batch or a hosted API.", styles["TableCell"])],
    [Paragraph("Orchestrator", styles["TableCellBold"]),
     Paragraph("Plans across days, routes to specialist agents, asks the owner before any filing/consent action.", styles["TableCell"])],
    [Paragraph("Ledger / Compliance / Scheme / Credit / Document / Follow-up", styles["TableCellBold"]),
     Paragraph("The six specialist agents: bill capture, GST/e-way prep, cited scheme matching, explainable credit score, loan packet assembly, nudges.", styles["TableCell"])],
    [Paragraph("Verifier gate", styles["TableCellBold"]),
     Paragraph("Rejects any output with no citation, an expired rule version, or no active consent token.", styles["TableCell"])],
    [Paragraph("Nightly Batch Brain", styles["TableCellBold"]),
     Paragraph("The supercomputer, used only off-hours: fine-tuning, bulk OCR, nightly re-scoring, synthetic data. Never serves live traffic.", styles["TableCell"])],
    [Paragraph("Karigar (agent factory)", styles["TableCellBold"]),
     Paragraph("Meta-agent that drafts a new specialist agent + eval set when a coverage gap appears, and self-tests it before it goes live.", styles["TableCell"])],
]
t = Table(arch_rows, colWidths=[48*mm, 108*mm])
t.setStyle(TableStyle([
    ("GRID", (0,0), (-1,-1), 0.5, LINE),
    ("BACKGROUND", (0,0), (-1,0), PANEL),
    ("VALIGN", (0,0), (-1,-1), "TOP"),
    ("TOPPADDING", (0,0), (-1,-1), 6),
    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ("LEFTPADDING", (0,0), (-1,-1), 8),
]))
story.append(t)

# ---------- 3. HARDWARE ----------
story.append(Paragraph("3. Hardware &mdash; PS-BOSS-OS (confirmed live, 2026-11-16)", styles["H2"]))
hw_rows = [
    [Paragraph("Resource", styles["TableHead"]), Paragraph("Confirmed spec", styles["TableHead"]), Paragraph("Our usage rule", styles["TableHead"])],
    [Paragraph("GPU 0", styles["TableCell"]), Paragraph("RTX 5000 Ada, 32GB VRAM (free)", styles["TableCell"]), Paragraph("Live serving capped at ~15GB budget", styles["TableCell"])],
    [Paragraph("GPU 1", styles["TableCell"]), Paragraph("RTX A400, 4GB", styles["TableCell"]), Paragraph("Not used by default", styles["TableCell"])],
    [Paragraph("CPU / RAM", styles["TableCell"]), Paragraph("2x Xeon Gold 6240R, 48 threads / 187GB (176GB free)", styles["TableCell"]), Paragraph("Capped at ~45GB for our jobs", styles["TableCell"])],
    [Paragraph("Disk", styles["TableCell"]), Paragraph("12TB free on /home", styles["TableCell"]), Paragraph("All project data under /home/admin/munimai", styles["TableCell"])],
    [Paragraph("Scheduler", styles["TableCell"]), Paragraph("Slurm, partition Standard*, idle", styles["TableCell"]), Paragraph("All heavy/long jobs go through sbatch, not bare SSH", styles["TableCell"])],
    [Paragraph("Access", styles["TableCell"]), Paragraph("Tailscale SSH, ACL set to accept", styles["TableCell"]), Paragraph("Unattended agent access confirmed working", styles["TableCell"])],
    [Paragraph("Root", styles["TableCell"]), Paragraph("No sudo available to us", styles["TableCell"]), Paragraph("User-space installs only (conda-forge), no system changes", styles["TableCell"])],
]
t2 = Table(hw_rows, colWidths=[28*mm, 75*mm, 53*mm])
t2.setStyle(TableStyle([
    ("GRID", (0,0), (-1,-1), 0.5, LINE),
    ("BACKGROUND", (0,0), (-1,0), PANEL),
    ("VALIGN", (0,0), (-1,-1), "TOP"),
    ("TOPPADDING", (0,0), (-1,-1), 6),
    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ("LEFTPADDING", (0,0), (-1,-1), 8),
    ("FONTSIZE", (0,0), (-1,-1), 8.5),
]))
story.append(t2)
story.append(Paragraph(
    "Important: this is a shared teaching/lab machine (faculty01, faculty02 accounts present; "
    "JupyterHub and MLflow run as standing services). It also reboots periodically, roughly "
    "every few days to a week, likely by another admin with console access &mdash; not sleep/suspend "
    "(confirmed disabled). Every long-running job is therefore submitted via Slurm, which can "
    "requeue on node restart, rather than run as a bare foreground process.", styles["BodySmall"]))

# ---------- 4. BUILD STATUS ----------
story.append(Paragraph("4. Build status &mdash; what exists right now", styles["H2"]))
status_rows = [
    [Paragraph("Step", styles["TableHead"]), Paragraph("Item", styles["TableHead"]), Paragraph("Status", styles["TableHead"])],
    [Paragraph("0", styles["TableCell"]), Paragraph("SSH access to PS-BOSS-OS (Tailscale, unattended)", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("0", styles["TableCell"]), Paragraph("Hardware/network audit (GPU, RAM, disk, Slurm, other users)", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("1", styles["TableCell"]), Paragraph("Serving engine: vLLM 0.21.0 + torch 2.11/cu130 (pre-built env, GPU verified)", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("1", styles["TableCell"]), Paragraph("Base model downloaded: Qwen2.5-7B-Instruct-AWQ (~6GB, 4-bit)", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("1", styles["TableCell"]), Paragraph("Project directory created: /home/admin/munimai/{models,data,logs,configs,scripts}", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("2", styles["TableCell"]), Paragraph("Container runtime (Podman rootless)", styles["TableCell"]), status_pill("BLOCKED", "#B8862E")],
    [Paragraph("2", styles["TableCell"]), Paragraph("Database: Postgres 16.16 + pgvector 0.7.4, conda-forge, user-space", styles["TableCell"]), status_pill("DONE", "#2E6E5E")],
    [Paragraph("3", styles["TableCell"]), Paragraph("Initialize Postgres data directory + start server on a local port", styles["TableCell"]), status_pill("NOT STARTED", "#5B5F52")],
    [Paragraph("4", styles["TableCell"]), Paragraph("vLLM smoke test: load Qwen2.5-7B-AWQ, cap VRAM to budget, serve one completion", styles["TableCell"]), status_pill("NOT STARTED", "#5B5F52")],
    [Paragraph("5", styles["TableCell"]), Paragraph("LangGraph + LangChain install for the orchestrator", styles["TableCell"]), status_pill("NOT STARTED", "#5B5F52")],
    [Paragraph("6", styles["TableCell"]), Paragraph("Slurm sbatch template for safe long-running jobs", styles["TableCell"]), status_pill("NOT STARTED", "#5B5F52")],
]
t3 = Table(status_rows, colWidths=[12*mm, 118*mm, 26*mm], repeatRows=1)
t3.setStyle(TableStyle([
    ("GRID", (0,0), (-1,-1), 0.5, LINE),
    ("BACKGROUND", (0,0), (-1,0), PANEL),
    ("VALIGN", (0,0), (-1,-1), "TOP"),
    ("TOPPADDING", (0,0), (-1,-1), 6),
    ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ("LEFTPADDING", (0,0), (-1,-1), 6),
    ("FONTSIZE", (0,0), (-1,-1), 8.5),
]))
story.append(t3)
story.append(Paragraph(
    "Note on Podman: rootless containers are blocked by two binaries (newuidmap, newgidmap) "
    "missing the setuid bit on this machine &mdash; a one-line fix (chmod u+s) that needs root, "
    "which we do not have. We pivoted to a conda-forge Postgres install instead, which needed "
    "no root and succeeded.", styles["BodySmall"]))

story.append(PageBreak())

# ---------- 5. NEXT STEPS ----------
story.append(Paragraph("5. Next steps (in order, one check-in after each)", styles["H2"]))
steps = [
    ("Step 3", "Initialize the Postgres data directory under /home/admin/munimai/data/pg, start the "
               "server bound to a local-only port, create the munimai database, enable the pgvector "
               "extension, confirm a test table + vector column works."),
    ("Step 4", "Smoke-test the model: start vLLM serving Qwen2.5-7B-Instruct-AWQ with "
               "gpu_memory_utilization capped so VRAM stays under the 15GB budget, send one test "
               "prompt, confirm a real completion comes back and measure latency."),
    ("Step 5", "Install LangGraph + LangChain into a new munimai-agents conda env (cloned from the "
               "existing torch install, so CUDA is not re-downloaded). Stub the orchestrator and one "
               "specialist agent (Ledger) end to end against the vLLM endpoint."),
    ("Step 6", "Write one reusable Slurm sbatch script template, used for every future long job "
               "(fine-tuning, batch OCR, nightly re-scoring) so a mid-run reboot by another admin "
               "does not silently lose hours of GPU work."),
    ("Step 7+", "Only after 3-6 are confirmed working: WhatsApp Business API integration, the "
               "remaining five specialist agents, the verifier gate, Karigar (agent factory), and "
               "the fairness/explainability harness."),
]
for label, text in steps:
    story.append(Paragraph(f"<b>{label}.</b> {text}", styles["Body"]))

story.append(Paragraph("6. Guardrails that stay in force throughout", styles["H2"]))
guardrails = [
    "No sudo / root use at any point; every install is user-space (conda-forge) or an approved alternative.",
    "No access to, or changes in, faculty01 / faculty02 home directories or their running services (JupyterHub, MLflow).",
    "All long-running work goes through Slurm sbatch, never a bare background SSH process, given the box's observed reboot pattern.",
    "Live serving stays inside the 15GB VRAM / 45GB RAM budget; the supercomputer is never the production server, only the batch/training factory.",
    "GST filing and loan submission are simulated throughout, per the competition's prepare-vs-file boundary; the owner approves every real-world action.",
    "A written check-in (like this document) precedes each new build step, until told otherwise.",
]
story.append(ListFlowable(
    [ListItem(Paragraph(g, styles["Body"]), leftIndent=4) for g in guardrails],
    bulletType="bullet", start="circle", leftIndent=14,
))

hr()
story.append(Paragraph(
    "MunimAI build plan &middot; generated for review before proceeding &middot; Tech4Bharat 2026, PS 3.2 (3.1 inside)",
    styles["Footer"]))

doc.build(story)
print("PDF written.")
