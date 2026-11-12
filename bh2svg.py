#!/usr/bin/env python3
"""Render a BloodHound CE graph JSON export as a BloodHound-styled SVG.

Usage:
  python3 bh2svg.py --json bh-graph.json --output graph.svg
  python3 bh2svg.py --json bh-graph.json --output graph.svg --rankdir TB

Nodes are drawn like BloodHound CE: coloured circles with the same Font Awesome
icon per node type, a skull badge on owned objects and a gem badge on Tier Zero.
Layout uses Graphviz (`dot`) when it is on PATH; otherwise a built-in layered
layout is used, so the script has no hard dependencies beyond Python 3.8+.

Icons: Font Awesome Free (https://fontawesome.com), CC BY 4.0.
"""
import argparse
import json
import math
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from html import escape
from pathlib import Path

# --- BloodHound CE look ------------------------------------------------------

# kind -> (fill colour, Font Awesome icon)
STYLE = {
    # Active Directory
    "User": ("#17E625", "user"), "Group": ("#DBE617", "users"), "Computer": ("#E67873", "desktop"),
    "Domain": ("#17E6B9", "globe"), "GPO": ("#998E4C", "list"), "OU": ("#FFAA00", "sitemap"),
    "Container": ("#F79A78", "box"), "AIACA": ("#9769F0", "arrows-left-right"),
    "RootCA": ("#6968E8", "landmark"), "EnterpriseCA": ("#4696E9", "building"),
    "NTAuthStore": ("#D575F5", "store"), "CertTemplate": ("#B153F3", "id-card"),
    "IssuancePolicy": ("#99B2DD", "clipboard-check"), "LocalUser": ("#17E625", "user-gear"),
    "LocalGroup": ("#DBE617", "users-rectangle"),
    # Azure / Entra ID
    "AZUser": ("#34D2EB", "user"), "AZGroup": ("#F57C9B", "users"), "AZTenant": ("#54F2F2", "cloud"),
    "AZSubscription": ("#D2CCA1", "key"), "AZResourceGroup": ("#89BD9E", "cube"),
    "AZVM": ("#F9ADA0", "desktop"), "AZDevice": ("#B18FCF", "desktop"), "AZKeyVault": ("#ED658C", "lock"),
    "AZApp": ("#03FC84", "window-restore"), "AZServicePrincipal": ("#C1D6D6", "robot"),
    "AZRole": ("#ED8537", "clipboard-list"), "AZManagementGroup": ("#BD93D8", "sitemap"),
    "AZWebApp": ("#4696E9", "server"), "AZFunctionApp": ("#F4BA44", "bolt"),
    "AZLogicApp": ("#9EE047", "gears"), "AZAutomationAccount": ("#F4BA44", "gears"),
    "AZContainerRegistry": ("#0885D7", "box-archive"), "AZManagedCluster": ("#326CE5", "cubes"),
    "AZVMScaleSet": ("#007CD0", "server"),
}
DEFAULT_STYLE = ("#DDE3EA", "question")
IGNORED_KINDS = {"Base", "AZBase", "Entity"}

R = 22            # node circle radius (px)
ICON = 24         # icon box size (px)
FONT = 11         # node label font size
EFONT = 9         # edge label font size
CHAR_W = 0.6      # approx. glyph width as a fraction of font size (Helvetica-ish)

ICONS = {
    'arrows-left-right': ('0 0 576 512', 'm470.6 374.6 96-96c12.5-12.5 12.5-32.8 0-45.3l-96-96c-12.5-12.5-32.8-12.5-45.3 0s-12.5 32.8 0 45.3l41.4 41.4H109.2l41.4-41.4c12.5-12.5 12.5-32.8 0-45.3s-32.8-12.5-45.3 0l-96 96c-6 6-9.4 14.1-9.4 22.6s3.4 16.6 9.4 22.6l96 96c12.5 12.5 32.8 12.5 45.3 0s12.5-32.8 0-45.3l-41.4-41.4h357.5l-41.4 41.4c-12.5 12.5-12.5 32.8 0 45.3s32.8 12.5 45.3 0z'),
    'bolt': ('0 0 448 512', 'M338.8-9.9c11.9 8.6 16.3 24.2 10.9 37.8L271.3 224H416c13.5 0 25.5 8.4 30.1 21.1s.7 26.9-9.6 35.5l-288 240c-11.3 9.4-27.4 9.9-39.3 1.3s-16.3-24.2-10.9-37.8L176.7 288H32c-13.5 0-25.5-8.4-30.1-21.1s-.7-26.9 9.6-35.5l288-240c11.3-9.4 27.4-9.9 39.3-1.3'),
    'box': ('0 0 448 512', 'm369.4 128-34.3-48H113l-34.3 48zM0 148.5c0-13.3 4.2-26.3 11.9-37.2l49-68.5C72.9 26 92.3 16 112.9 16H335c20.7 0 40.1 10 52.1 26.8l48.9 68.5c7.8 10.9 11.9 23.9 11.9 37.2L448 416c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64z'),
    'box-archive': ('0 0 512 512', 'M0 64c0-17.7 14.3-32 32-32h448c17.7 0 32 14.3 32 32v32c0 17.7-14.3 32-32 32H32c-17.7 0-32-14.3-32-32zm32 112h448v240c0 35.3-28.7 64-64 64H96c-35.3 0-64-28.7-64-64zm152 64c-13.3 0-24 10.7-24 24s10.7 24 24 24h144c13.3 0 24-10.7 24-24s-10.7-24-24-24z'),
    'building': ('0 0 384 512', 'M64 0C28.7 0 0 28.7 0 64v384c0 35.3 28.7 64 64 64h256c35.3 0 64-28.7 64-64V64c0-35.3-28.7-64-64-64zm112 352h32c17.7 0 32 14.3 32 32v80h-96v-80c0-17.7 14.3-32 32-32M96 112c0-8.8 7.2-16 16-16h32c8.8 0 16 7.2 16 16v32c0 8.8-7.2 16-16 16h-32c-8.8 0-16-7.2-16-16zm144-16h32c8.8 0 16 7.2 16 16v32c0 8.8-7.2 16-16 16h-32c-8.8 0-16-7.2-16-16v-32c0-8.8 7.2-16 16-16M96 240c0-8.8 7.2-16 16-16h32c8.8 0 16 7.2 16 16v32c0 8.8-7.2 16-16 16h-32c-8.8 0-16-7.2-16-16zm144-16h32c8.8 0 16 7.2 16 16v32c0 8.8-7.2 16-16 16h-32c-8.8 0-16-7.2-16-16v-32c0-8.8 7.2-16 16-16'),
    'certificate': ('0 0 576 512', 'M239.2-8c-6.1-6.2-15-8.7-23.4-6.4s-14.9 8.8-17 17.2L183.5 63c-1.1 4.4-5.6 7-9.9 5.7l-59.8-16.8c-8.4-2.4-17.4 0-23.5 6.1s-8.5 15.1-6.1 23.5l16.9 59.8c1.2 4.3-1.4 8.8-5.7 9.9l-60.3 15.3c-8.4 2.1-15 8.7-17.3 17.1s.2 17.3 6.4 23.4l44.5 43.3c3.2 3.1 3.2 8.3 0 11.5l-44.4 43.3c-6.2 6.1-8.7 15-6.4 23.4s8.9 14.9 17.3 17.1l60.2 15.3c4.4 1.1 7 5.6 5.7 9.9l-16.9 59.7c-2.4 8.4 0 17.4 6.1 23.5s15.1 8.5 23.5 6.1l59.8-16.9c4.3-1.2 8.8 1.4 9.9 5.7l15.3 60.2c2.1 8.4 8.7 15 17.1 17.3s17.3-.2 23.4-6.4l43.3-44.5c3.1-3.2 8.3-3.2 11.5 0l43.2 44.5c6.1 6.2 15 8.7 23.4 6.4s14.9-8.9 17.1-17.3l15.3-60.1c1.1-4.4 5.6-7 9.9-5.7l59.8 16.9c8.4 2.4 17.4 0 23.5-6.1s8.5-15.1 6.1-23.5l-16.9-59.8c-1.2-4.3 1.4-8.8 5.7-9.9l60.2-15.3c8.4-2.1 15-8.7 17.3-17.1s-.2-17.4-6.4-23.4l-44.5-43.3c-3.2-3.1-3.2-8.3 0-11.5l44.5-43.3c6.2-6.1 8.7-15 6.4-23.4s-8.9-14.9-17.3-17.1l-60.2-15.3c-4.4-1.1-7-5.6-5.7-9.9l16.9-59.8c2.4-8.4 0-17.4-6.1-23.5s-15.1-8.5-23.5-6.1L403 68.8c-4.3 1.2-8.8-1.4-9.9-5.7L377.8 2.8c-2.1-8.4-8.7-15-17.1-17.3s-17.3.2-23.4 6.4L294 36.5c-3.1 3.2-8.3 3.2-11.5 0z'),
    'clipboard-check': ('0 0 384 512', 'M256 0c23.7 0 44.4 12.9 55.4 32h8.6c35.3 0 64 28.7 64 64v352c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V96c0-35.3 28.7-64 64-64h8.6c11-19.1 31.7-32 55.4-32zm26.9 212.6c-10.7-7.8-25.7-5.4-33.5 5.3l-85.6 117.7-26.5-27.4c-9.2-9.5-24.4-9.8-33.9-.6s-9.8 24.4-.6 33.9l46.4 48c4.9 5.1 11.8 7.8 18.9 7.3s13.6-4.1 17.8-9.8l102.3-140.9c7.8-10.7 5.4-25.7-5.3-33.5M136 64c-13.3 0-24 10.7-24 24s10.7 24 24 24h112c13.3 0 24-10.7 24-24s-10.7-24-24-24z'),
    'clipboard-list': ('0 0 384 512', 'M311.4 32h8.6c35.3 0 64 28.7 64 64v352c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V96c0-35.3 28.7-64 64-64h8.6c11-19.1 31.7-32 55.4-32h128c23.7 0 44.4 12.9 55.4 32M248 112c13.3 0 24-10.7 24-24s-10.7-24-24-24H136c-13.3 0-24 10.7-24 24s10.7 24 24 24zM128 256a32 32 0 1 0-64 0 32 32 0 1 0 64 0m32 0c0 13.3 10.7 24 24 24h112c13.3 0 24-10.7 24-24s-10.7-24-24-24H184c-13.3 0-24 10.7-24 24m0 128c0 13.3 10.7 24 24 24h112c13.3 0 24-10.7 24-24s-10.7-24-24-24H184c-13.3 0-24 10.7-24 24m-64 32a32 32 0 1 0 0-64 32 32 0 1 0 0 64'),
    'cloud': ('0 0 576 512', 'M0 336c0 79.5 64.5 144 144 144h304c70.7 0 128-57.3 128-128 0-51.6-30.5-96.1-74.5-116.3 6.7-13.1 10.5-28 10.5-43.7 0-53-43-96-96-96-17.7 0-34.2 4.8-48.4 13.1C343.5 63.3 295.4 32 240 32 160.5 32 96 96.5 96 176c0 8 .7 15.9 1.9 23.5C41 218.7 0 272.6 0 336'),
    'cube': ('0 0 512 512', 'M224.3-2.5c19.8-11.4 44.2-11.4 64 0L464.2 99c19.8 11.4 32 32.6 32 55.4v203c0 22.9-12.2 44-32 55.4L288.3 514.5a64.16 64.16 0 0 1-64 0L48.5 413c-19.8-11.4-32-32.6-32-55.4v-203c0-22.9 12.2-44 32-55.4zm207.8 360V191.4l-143.8 83v166.1z'),
    'cubes': ('0 0 512 512', 'M284-1.3c-17.3-10-38.7-10-56 0l-84.2 48.6c-17.3 10-28 28.5-28 48.5v101.9l-88.3 51c-17.3 10-28 28.5-28 48.5v97.3c0 20 10.7 38.5 28 48.5l84.3 48.6c17.3 10 38.7 10 56 0l88.3-51 88.3 51c17.3 10 38.7 10 56 0l84.1-48.6c17.3-10 28-28.5 28-48.5v-97.3c0-20-10.7-38.5-28-48.5l-88.3-51V95.8c0-20-10.7-38.5-28-48.5zm-52 293.9v106.5l-88.3 51c-1.2.7-2.6 1.1-4 1.1V345.9zm231.4.6c.7 1.2 1.1 2.6 1.1 4v97.3c0 2.9-1.5 5.5-4 6.9L376.2 450c-1.2.7-2.6 1.1-4 1.1V345.8zM348.3 95.8v101.9L256 251V144.5l91.2-52.6c.7 1.2 1.1 2.6 1.1 4z'),
    'desktop': ('0 0 512 512', 'M64 32C28.7 32 0 60.7 0 96v256c0 35.3 28.7 64 64 64h144l-16 48h-72c-13.3 0-24 10.7-24 24s10.7 24 24 24h272c13.3 0 24-10.7 24-24s-10.7-24-24-24h-72l-16-48h144c35.3 0 64-28.7 64-64V96c0-35.3-28.7-64-64-64zm32 64h320c17.7 0 32 14.3 32 32v160c0 17.7-14.3 32-32 32H96c-17.7 0-32-14.3-32-32V128c0-17.7 14.3-32 32-32'),
    'gears': ('0 0 640 512', 'M415.9 210.5c12.2-3.3 25 2.5 30.5 13.8l18.6 37.6c10.3 1.4 20.4 4.2 29.9 8.1l35-23.3c10.5-7 24.4-5.6 33.3 3.3l19.2 19.2c8.9 8.9 10.3 22.9 3.3 33.3l-23.3 34.9c1.9 4.7 3.6 9.6 5 14.7s2.3 10.1 3 15.2l37.7 18.6c11.3 5.6 17.1 18.4 13.8 30.5l-7 26.2c-3.3 12.1-14.6 20.3-27.2 19.5l-42-2.7c-6.3 8.1-13.6 15.6-21.9 22l2.7 41.9c.8 12.6-7.4 24-19.5 27.2l-26.2 7c-12.2 3.3-24.9-2.5-30.5-13.8l-18.6-37.6c-10.3-1.4-20.4-4.2-29.9-8.1l-35 23.3c-10.5 7-24.4 5.6-33.3-3.3l-19.2-19.2c-8.9-8.9-10.3-22.8-3.3-33.3l23.3-35c-1.9-4.7-3.6-9.6-5-14.7s-2.3-10.2-3-15.2L288.6 382c-11.3-5.6-17-18.4-13.8-30.5l7-26.2c3.3-12.1 14.6-20.3 27.2-19.5l41.9 2.7c6.3-8.1 13.6-15.6 21.9-22l-2.7-41.8c-.8-12.6 7.4-24 19.5-27.2l26.2-7zM448.4 340a44 44 0 1 0 .1 88 44 44 0 1 0-.1-88M224.9-45.5l26.2 7c12.1 3.3 20.3 14.7 19.5 27.2l-2.7 41.8c8.3 6.4 15.6 13.8 21.9 22l42-2.7c12.5-.8 23.9 7.4 27.2 19.5l7 26.2c3.2 12.1-2.5 24.9-13.8 30.5l-37.7 18.6c-.7 5.1-1.7 10.2-3 15.2s-3.1 10-5 14.7l23.3 35c7 10.5 5.6 24.4-3.3 33.3L307.3 262c-8.9 8.9-22.8 10.3-33.3 3.3L239 242c-9.5 3.9-19.6 6.7-29.9 8.1l-18.6 37.6c-5.6 11.3-18.4 17-30.5 13.8l-26.2-7c-12.2-3.3-20.3-14.7-19.5-27.2l2.7-41.9c-8.3-6.4-15.6-13.8-21.9-22l-42 2.7c-12.5.8-23.9-7.4-27.2-19.5l-7-26.2c-3.2-12.1 2.5-24.9 13.8-30.5l37.7-18.6c.7-5.1 1.7-10.1 3-15.2 1.4-5.1 3-10 5-14.7L55.1 46.5c-7-10.5-5.6-24.4 3.3-33.3L77.6-6c8.9-8.9 22.8-10.3 33.3-3.3l35 23.3c9.5-3.9 19.6-6.7 29.9-8.1l18.6-37.6c5.6-11.3 18.3-17 30.5-13.8M192.4 84a44 44 0 1 0 0 88 44 44 0 1 0 0-88'),
    'gem': ('0 0 512 512', 'M116.7 33.8c4.5-6.1 11.7-9.8 19.3-9.8h240c7.6 0 14.8 3.6 19.3 9.8l112 152c6.8 9.2 6.1 21.9-1.5 30.4l-232 256c-4.5 5-11 7.9-17.8 7.9s-13.2-2.9-17.8-7.9l-232-256c-7.7-8.5-8.3-21.2-1.5-30.4zm38.5 39.8c-3.3 2.5-4.2 7-2.1 10.5l57.4 95.7L63.3 192c-4.1.3-7.3 3.8-7.3 8s3.2 7.6 7.3 8l192 16h1.3l192-16c4.1-.3 7.3-3.8 7.3-8s-3.2-7.6-7.3-8l-147.2-12.3 57.4-95.6c2.1-3.5 1.2-8.1-2.1-10.5s-7.9-2-10.7 1l-90 97.6-90.1-97.6c-2.8-3-7.4-3.4-10.7-1'),
    'globe': ('0 0 512 512', 'M351.9 280H161c2.9 64.5 17.2 123.9 37.5 167.4 11.4 24.5 23.7 41.8 35.1 52.4 11.2 10.5 18.9 12.2 22.9 12.2s11.7-1.7 22.9-12.2c11.4-10.6 23.7-28 35.1-52.4 20.3-43.5 34.6-102.9 37.5-167.4zm-191-48h190.9c-2.8-64.5-17.1-123.9-37.4-167.4-11.4-24.4-23.7-41.8-35.1-52.4C268.1 1.7 260.4 0 256.4 0s-11.7 1.7-22.9 12.2c-11.4 10.6-23.7 28-35.1 52.4-20.3 43.5-34.6 102.9-37.5 167.4m-48 0c3.5-85.6 25.6-165.1 57.9-217.3C78.7 47.3 10.9 131.2 1.5 232zM1.5 280c9.4 100.8 77.2 184.7 169.3 217.3-32.3-52.2-54.4-131.7-57.9-217.3zm398.4 0c-3.5 85.6-25.6 165.1-57.9 217.3 92.1-32.7 159.9-116.5 169.3-217.3zm111.4-48C501.9 131.2 434.1 47.3 342 14.7c32.3 52.2 54.4 131.7 57.9 217.3z'),
    'id-card': ('0 0 576 512', 'M0 96c0-35.3 28.7-64 64-64h448c35.3 0 64 28.7 64 64zm0 48h576v272c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64zm247.3 272c20.2 0 35.3-19.4 22.4-35-14.7-17.7-36.9-29-61.7-29h-64c-24.8 0-47 11.3-61.7 29-12.9 15.6 2.2 35 22.4 35h142.5zM176 312a56 56 0 1 0 0-112 56 56 0 1 0 0 112m184-104c-13.3 0-24 10.7-24 24s10.7 24 24 24h112c13.3 0 24-10.7 24-24s-10.7-24-24-24zm0 96c-13.3 0-24 10.7-24 24s10.7 24 24 24h112c13.3 0 24-10.7 24-24s-10.7-24-24-24z'),
    'key': ('0 0 512 512', 'M336 352c97.2 0 176-78.8 176-176S433.2 0 336 0 160 78.8 160 176c0 18.7 2.9 36.8 8.3 53.7L7 391c-4.5 4.5-7 10.6-7 17v80c0 13.3 10.7 24 24 24h80c13.3 0 24-10.7 24-24v-40h40c13.3 0 24-10.7 24-24v-40h40c6.4 0 12.5-2.5 17-7l33.3-33.3c16.9 5.4 35 8.3 53.7 8.3m40-256a40 40 0 1 1 0 80 40 40 0 1 1 0-80'),
    'landmark': ('0 0 512 512', 'M238.7 5.1c10.5-6.8 24.1-6.8 34.6 0l224 144c11.9 7.7 17.4 22.3 13.4 35.9s-16.5 23-30.7 23h-32v208l51.2 38.4c8.1 6 12.8 15.5 12.8 25.6 0 17.7-14.3 32-32 32H32c-17.7 0-32-14.3-32-32 0-10.1 4.7-19.6 12.8-25.6L64 416V208H32c-14.2 0-26.7-9.4-30.7-23s1.5-28.3 13.4-35.9zM336 208v208h64V208zM224 416h64V208h-64zM112 208v208h64V208z'),
    'list': ('0 0 512 512', 'M40 48c-13.3 0-24 10.7-24 24v48c0 13.3 10.7 24 24 24h48c13.3 0 24-10.7 24-24V72c0-13.3-10.7-24-24-24zm152 16c-17.7 0-32 14.3-32 32s14.3 32 32 32h288c17.7 0 32-14.3 32-32s-14.3-32-32-32zm0 160c-17.7 0-32 14.3-32 32s14.3 32 32 32h288c17.7 0 32-14.3 32-32s-14.3-32-32-32zm0 160c-17.7 0-32 14.3-32 32s14.3 32 32 32h288c17.7 0 32-14.3 32-32s-14.3-32-32-32zM16 232v48c0 13.3 10.7 24 24 24h48c13.3 0 24-10.7 24-24v-48c0-13.3-10.7-24-24-24H40c-13.3 0-24 10.7-24 24m24 136c-13.3 0-24 10.7-24 24v48c0 13.3 10.7 24 24 24h48c13.3 0 24-10.7 24-24v-48c0-13.3-10.7-24-24-24z'),
    'lock': ('0 0 384 512', 'M128 96v64h128V96c0-35.3-28.7-64-64-64s-64 28.7-64 64m-64 64V96c0-70.7 57.3-128 128-128S320 25.3 320 96v64c35.3 0 64 28.7 64 64v224c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64V224c0-35.3 28.7-64 64-64'),
    'question': ('0 0 320 512', 'M64 160c0-53 43-96 96-96s96 43 96 96c0 42.7-27.9 78.9-66.5 91.4-28.4 9.2-61.5 35.3-61.5 76.6v24c0 17.7 14.3 32 32 32s32-14.3 32-32v-24c0-1.7.6-4.1 3.5-7.3 3-3.3 7.9-6.5 13.7-8.4C273.5 291.6 320 231.3 320 160 320 71.6 248.4 0 160 0S0 71.6 0 160c0 17.7 14.3 32 32 32s32-14.3 32-32m96 352c22.1 0 40-17.9 40-40s-17.9-40-40-40-40 17.9-40 40 17.9 40 40 40'),
    'robot': ('0 0 640 512', 'M352 0c0-17.7-14.3-32-32-32s-32 14.3-32 32v64h-96c-53 0-96 43-96 96v224c0 53 43 96 96 96h256c53 0 96-43 96-96V160c0-53-43-96-96-96h-96zM160 368c0-13.3 10.7-24 24-24h32c13.3 0 24 10.7 24 24s-10.7 24-24 24h-32c-13.3 0-24-10.7-24-24m120 0c0-13.3 10.7-24 24-24h32c13.3 0 24 10.7 24 24s-10.7 24-24 24h-32c-13.3 0-24-10.7-24-24m120 0c0-13.3 10.7-24 24-24h32c13.3 0 24 10.7 24 24s-10.7 24-24 24h-32c-13.3 0-24-10.7-24-24M224 176a48 48 0 1 1 0 96 48 48 0 1 1 0-96m144 48a48 48 0 1 1 96 0 48 48 0 1 1-96 0m-304 0c0-17.7-14.3-32-32-32S0 206.3 0 224v96c0 17.7 14.3 32 32 32s32-14.3 32-32zm544-32c-17.7 0-32 14.3-32 32v96c0 17.7 14.3 32 32 32s32-14.3 32-32v-96c0-17.7-14.3-32-32-32'),
    'server': ('0 0 448 512', 'M64 32C28.7 32 0 60.7 0 96v64c0 35.3 28.7 64 64 64h320c35.3 0 64-28.7 64-64V96c0-35.3-28.7-64-64-64zm216 72a24 24 0 1 1 0 48 24 24 0 1 1 0-48m56 24a24 24 0 1 1 48 0 24 24 0 1 1-48 0M64 288c-35.3 0-64 28.7-64 64v64c0 35.3 28.7 64 64 64h320c35.3 0 64-28.7 64-64v-64c0-35.3-28.7-64-64-64zm216 72a24 24 0 1 1 0 48 24 24 0 1 1 0-48m56 24a24 24 0 1 1 48 0 24 24 0 1 1-48 0'),
    'sitemap': ('0 0 512 512', 'M192 64c0-17.7 14.3-32 32-32h64c17.7 0 32 14.3 32 32v64c0 17.7-14.3 32-32 32h-8v64h120c39.8 0 72 32.2 72 72v56h8c17.7 0 32 14.3 32 32v64c0 17.7-14.3 32-32 32h-64c-17.7 0-32-14.3-32-32v-64c0-17.7 14.3-32 32-32h8v-56c0-13.3-10.7-24-24-24H280v80h8c17.7 0 32 14.3 32 32v64c0 17.7-14.3 32-32 32h-64c-17.7 0-32-14.3-32-32v-64c0-17.7 14.3-32 32-32h8v-80H112c-13.3 0-24 10.7-24 24v56h8c17.7 0 32 14.3 32 32v64c0 17.7-14.3 32-32 32H32c-17.7 0-32-14.3-32-32v-64c0-17.7 14.3-32 32-32h8v-56c0-39.8 32.2-72 72-72h120v-64h-8c-17.7 0-32-14.3-32-32z'),
    'skull': ('0 0 512 512', 'M416 427.4c58.5-44 96-111.6 96-187.4C512 107.5 397.4 0 256 0S0 107.5 0 240c0 75.8 37.5 143.4 96 187.4V464c0 26.5 21.5 48 48 48h32v-40c0-13.3 10.7-24 24-24s24 10.7 24 24v40h64v-40c0-13.3 10.7-24 24-24s24 10.7 24 24v40h32c26.5 0 48-21.5 48-48zM96 256a64 64 0 1 1 128 0 64 64 0 1 1-128 0m256-64a64 64 0 1 1 0 128 64 64 0 1 1 0-128'),
    'store': ('0 0 512 512', 'M30.7 72.3C37.6 48.4 59.5 32 84.4 32h344c24.9 0 46.8 16.4 53.8 40.3l23.4 80.2c12.8 43.7-20.1 87.5-65.6 87.5-26.3 0-49.4-14.9-60.8-37.1-11.6 21.9-34.6 37.1-61.4 37.1-26.6 0-49.7-15-61.3-37-11.6 22-34.7 37-61.3 37-26.8 0-49.8-15.1-61.4-37.1C122.4 225 99.3 240 73 240c-45.6 0-78.4-43.7-65.6-87.5zM96.4 352h320v-66.4c7.6 1.6 15.5 2.4 23.5 2.4 14.3 0 28-2.6 40.5-7.2V432c0 26.5-21.5 48-48 48h-352c-26.5 0-48-21.5-48-48V280.8c12.5 4.6 26.1 7.2 40.5 7.2 8.1 0 15.9-.8 23.5-2.4z'),
    'user': ('0 0 448 512', 'M224 248a120 120 0 1 0 0-240 120 120 0 1 0 0 240m-29.7 56C95.8 304 16 383.8 16 482.3c0 16.4 13.3 29.7 29.7 29.7h356.6c16.4 0 29.7-13.3 29.7-29.7 0-98.5-79.8-178.3-178.3-178.3z'),
    'user-gear': ('0 0 640 512', 'M256.5 8a120 120 0 1 1 0 240 120 120 0 1 1 0-240m-29.8 296h60.9c-12.9 26.8-7.8 58.2 11.5 79.5-20.2 22.3-24.8 55.8-9.4 83.4l22.5 40.4c.9 1.6 1.9 3.2 2.9 4.7h-237c-16.4 0-29.7-13.3-29.7-29.7 0-98.5 79.8-178.3 178.3-178.3m205.9-56.4c0-13.3 10.7-24 24-24h48c13.3 0 24 10.7 24 24v6.1c0 18.9 24.1 32.8 40.5 23.4l5-2.9c11.6-6.7 26.5-2.6 33 9.1l22.4 40.2c6.2 11.2 2.6 25.2-8.2 32l-4.7 2.9c-16.2 10.1-16.2 39.9 0 50.1l4.6 2.9c10.8 6.8 14.5 20.8 8.3 32L607 483.8c-6.5 11.7-21.4 15.9-33 9.1l-4.9-2.9c-16.4-9.5-40.5 4.5-40.5 23.4v6.1c0 13.3-10.7 24-24 24h-48c-13.3 0-24-10.7-24-24v-5.9c0-19-24.2-33-40.7-23.5l-4.8 2.8c-11.6 6.7-26.4 2.6-33-9.1l-22.6-40.4c-6.2-11.2-2.6-25.3 8.3-32.1l4.4-2.7c16.3-10.1 16.3-40.1 0-50.2l-4.5-2.8c-10.9-6.8-14.5-20.9-8.3-32.1l22.5-40.3c6.5-11.7 21.4-15.8 32.9-9.1l4.8 2.8c16.5 9.5 40.7-4.5 40.7-23.5v-5.9zm99.9 136.2a52 52 0 1 0-104 0 52 52 0 1 0 104 0'),
    'users': ('0 0 640 512', 'M320 16a104 104 0 1 1 0 208 104 104 0 1 1 0-208M96 88a72 72 0 1 1 0 144 72 72 0 1 1 0-144M0 416c0-70.7 57.3-128 128-128 12.8 0 25.2 1.9 36.9 5.4C132 330.2 112 378.8 112 432v16c0 11.4 2.4 22.2 6.7 32H32c-17.7 0-32-14.3-32-32zm521.3 64c4.3-9.8 6.7-20.6 6.7-32v-16c0-53.2-20-101.8-52.9-138.6 11.7-3.5 24.1-5.4 36.9-5.4 70.7 0 128 57.3 128 128v32c0 17.7-14.3 32-32 32zM472 160a72 72 0 1 1 144 0 72 72 0 1 1-144 0M160 432c0-88.4 71.6-160 160-160s160 71.6 160 160v16c0 17.7-14.3 32-32 32H192c-17.7 0-32-14.3-32-32z'),
    'users-rectangle': ('0 0 576 512', 'M64 32C28.7 32 0 60.7 0 96v320c0 35.3 28.7 64 64 64h448c35.3 0 64-28.7 64-64V96c0-35.3-28.7-64-64-64zm224 72a56 56 0 1 1 0 112 56 56 0 1 1 0-112m0 152c53 0 96 43 96 96v24c0 13.3-10.7 24-24 24H216c-13.3 0-24-10.7-24-24v-24c0-53 43-96 96-96m96-64a48 48 0 1 1 96 0 48 48 0 1 1-96 0m-216 80.3c-15.2 22.8-24 50.2-24 79.7v24c0 8.4 1.4 16.5 4.1 24h-46.8c-11.7 0-21.3-9.6-21.3-21.3V368c0-50.3 38.7-91.6 88-95.7M427.9 400c2.7-7.5 4.1-15.6 4.1-24v-24c0-29.5-8.8-56.9-24-79.7 49.3 4.1 88 45.3 88 95.7v10.7c0 11.8-9.6 21.3-21.3 21.3zM96 192a48 48 0 1 1 96 0 48 48 0 1 1-96 0'),
    'window-restore': ('0 0 576 512', 'M512 96H160c0-35.3 28.7-64 64-64h288c35.3 0 64 28.7 64 64v192c0 35.3-28.7 64-64 64h-48v-64h48zM0 224c0-35.3 28.7-64 64-64h288c35.3 0 64 28.7 64 64v192c0 35.3-28.7 64-64 64H64c-35.3 0-64-28.7-64-64zm64 40c0 13.3 10.7 24 24 24h240c13.3 0 24-10.7 24-24s-10.7-24-24-24H88c-13.3 0-24 10.7-24 24'),
}


# --- Loading -----------------------------------------------------------------

def load(path):
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    nodes = data.get("nodes", {})
    edges = data.get("edges", data.get("relationships", []))
    if isinstance(nodes, list):
        nodes = {str(n.get("id") or n.get("objectId") or n.get("objectid") or i): n for i, n in enumerate(nodes)}
    if isinstance(edges, dict):
        edges = list(edges.values())
    nodes = {str(k): v for k, v in nodes.items()}
    out_edges = []
    for e in edges:
        src = e.get("source") or e.get("from") or e.get("start")
        dst = e.get("target") or e.get("to") or e.get("end")
        if src is None or dst is None or str(src) not in nodes or str(dst) not in nodes:
            continue
        out_edges.append((str(src), str(dst), str(e.get("label") or e.get("kind") or e.get("type") or "")))
    return raw, nodes, out_edges


def props(n):
    return n.get("properties") or n.get("data") or {}


def kinds(n):
    ks = n.get("kinds") if isinstance(n.get("kinds"), list) else []
    ks += [n.get("kind") or n.get("type") or ""]
    return [k for k in ks if k]


def node_kind(n):
    ks = kinds(n)
    return next((k for k in ks if k in STYLE), next((k for k in ks if k not in IGNORED_KINDS and not k.startswith("Tag_")), ""))


def node_label(nid, n):
    p = props(n)
    return str(n.get("label") or p.get("name") or n.get("name") or p.get("objectid") or nid)


def node_flags(n):
    p, ks = props(n), kinds(n)
    tags = " ".join(str(x) for x in (p.get("system_tags"), n.get("system_tags"), p.get("tags")) if x)
    owned = "Tag_Owned" in ks or "owned" in tags or p.get("owned") is True or n.get("isOwnedObject") is True
    tier0 = ("Tag_Tier_Zero" in ks or "admin_tier_0" in tags or p.get("highvalue") is True
             or n.get("isTierZero") is True)
    return owned, tier0


def text_w(s, size):
    return len(s) * size * CHAR_W


# --- Layout: Graphviz --------------------------------------------------------

def layout_graphviz(ids, labels, edges, rankdir):
    """Return node centres {id: (x, y)}, edge geometry [(points, label_xy)], size (w, h) in px."""
    idx = {nid: f"n{i}" for i, nid in enumerate(ids)}
    rev = {v: k for k, v in idx.items()}
    max_lbl = max((text_w(labels[n], FONT) for n in ids), default=0) / 72
    d = 2 * R / 72
    ranksep = max(1.2, max_lbl * 0.75) if rankdir in ("LR", "RL") else 1.1
    nodesep = 1.1 if rankdir in ("LR", "RL") else max(0.6, max_lbl * 0.8)
    src = [f"digraph G {{ rankdir={rankdir}; ranksep={ranksep:.2f}; nodesep={nodesep:.2f};",
           f'node [shape=circle, fixedsize=true, width={d:.3f}, label=""];',
           f'edge [arrowhead=none, fontname="Helvetica", fontsize={EFONT}];']
    src += [f"{idx[n]};" for n in ids]
    for s, t, lbl in edges:
        safe = lbl.replace('"', "'")
        src.append(f'{idx[s]} -> {idx[t]} [label="{safe}"];')
    src.append("}")
    res = subprocess.run(["dot", "-Tplain"], input="\n".join(src).encode(), capture_output=True, check=True)

    pos, geo, W, H = {}, [], 0, 0
    for line in res.stdout.decode().splitlines():
        f = shlex.split(line)
        if not f:
            continue
        if f[0] == "graph":
            W, H = float(f[2]) * 72, float(f[3]) * 72
        elif f[0] == "node":
            pos[rev[f[1]]] = (float(f[2]) * 72, H - float(f[3]) * 72)
        elif f[0] == "edge":
            n = int(f[3])
            pts = [(float(f[4 + 2 * i]) * 72, H - float(f[5 + 2 * i]) * 72) for i in range(n)]
            rest = f[4 + 2 * n:]
            has_lbl = len(rest) >= 5
            lxy = (float(rest[1]) * 72, H - float(rest[2]) * 72) if has_lbl else None
            geo.append((pts, lxy, rest[0] if has_lbl else ""))
    return pos, geo, (W, H)


# --- Layout: built-in fallback ----------------------------------------------

def layout_builtin(ids, labels, edges, rankdir):
    succ, pred = defaultdict(list), defaultdict(list)
    for s, t, _ in edges:
        if s != t:
            succ[s].append(t)
            pred[t].append(s)
    # Break cycles with DFS (drop back edges for ranking only).
    state, dag = {}, defaultdict(list)

    def dfs(u):
        state[u] = 1
        for v in succ[u]:
            if state.get(v) == 1:
                continue
            dag[u].append(v)
            if v not in state:
                dfs(v)
        state[u] = 2

    sys.setrecursionlimit(max(1000, len(ids) * 4))
    for n in ids:
        if n not in state:
            dfs(n)
    # Longest-path layering, then pull each node towards its successors.
    order, seen = [], set()

    def topo(u):
        seen.add(u)
        for v in dag[u]:
            if v not in seen:
                topo(v)
        order.append(u)

    for n in ids:
        if n not in seen:
            topo(n)
    order.reverse()
    rank = {n: 0 for n in ids}
    for u in order:
        for v in dag[u]:
            rank[v] = max(rank[v], rank[u] + 1)
    for u in reversed(order):
        if dag[u]:
            rank[u] = min(rank[v] for v in dag[u]) - 1
    lo = min(rank.values(), default=0)
    layers = defaultdict(list)
    for n in ids:
        layers[rank[n] - lo].append(n)
    # Barycentre ordering sweeps to reduce crossings.
    nl = max(layers) + 1 if layers else 0
    for _ in range(6):
        for r in list(range(1, nl)) + list(range(nl - 2, -1, -1)):
            ref = layers[r - 1] if r > 0 else []
            ref2 = layers[r + 1] if r + 1 < nl else []
            p = {n: i for i, n in enumerate(ref)}
            p2 = {n: i for i, n in enumerate(ref2)}

            def bary(n, i):
                nb = [p[x] for x in pred[n] if x in p] + [p2[x] for x in succ[n] if x in p2]
                return sum(nb) / len(nb) if nb else i
            layers[r] = [n for _, n in sorted(((bary(n, i), n) for i, n in enumerate(layers[r])), key=lambda t: t[0])]

    max_lbl = max((text_w(labels[n], FONT) for n in ids), default=0)
    horiz = rankdir in ("LR", "RL")
    rank_gap = max(170, max_lbl + 40) if horiz else 150
    item_gap = 95 if horiz else max(110, max_lbl + 30)
    widest = max((len(v) for v in layers.values()), default=1)
    pos = {}
    for r, members in layers.items():
        off = (widest - len(members)) * item_gap / 2
        for i, n in enumerate(members):
            a, b = r * rank_gap, off + i * item_gap
            pos[n] = (a, b) if horiz else (b, a)
    if rankdir in ("RL", "BT"):
        m = max((p[0 if rankdir == "RL" else 1] for p in pos.values()), default=0)
        pos = {n: ((m - x, y) if rankdir == "RL" else (x, m - y)) for n, (x, y) in pos.items()}

    geo, pair_total, pair_seen = [], defaultdict(int), defaultdict(int)
    for s, t, _ in edges:
        pair_total[frozenset((s, t))] += 1
    for s, t, lbl in edges:
        (x1, y1), (x2, y2) = pos[s], pos[t]
        key = frozenset((s, t))
        k = pair_seen[key]
        pair_seen[key] += 1
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1
        ux, uy = dx / L, dy / L
        # Fan out parallel/reciprocal edges on alternating sides of a shared axis.
        bend = 0 if pair_total[key] == 1 else 24 * (k // 2 + 1) * (1 if k % 2 == 0 else -1)
        sign = 1 if s <= t else -1
        a = (x1 + ux * R, y1 + uy * R)
        b = (x2 - ux * R, y2 - uy * R)
        mx, my = (a[0] + b[0]) / 2 - uy * bend * sign, (a[1] + b[1]) / 2 + ux * bend * sign
        c1 = ((a[0] + mx) / 2, (a[1] + my) / 2)
        c2 = ((b[0] + mx) / 2, (b[1] + my) / 2)
        geo.append(([a, c1, c2, b], (mx, my), lbl))
    xs = [p[0] for p in pos.values()] or [0]
    ys = [p[1] for p in pos.values()] or [0]
    W, H = max(xs) + R, max(ys) + R
    return pos, geo, (W, H)


# --- SVG rendering -----------------------------------------------------------

def darker(hex_colour, f=0.65):
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#{:02x}{:02x}{:02x}".format(int(r * f), int(g * f), int(b * f))


def path_d(pts):
    d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(1, len(pts) - 2, 3):
        (a, b), (c, e), (g, h) = pts[i], pts[i + 1], pts[i + 2]
        d += f" C{a:.1f},{b:.1f} {c:.1f},{e:.1f} {g:.1f},{h:.1f}"
    return d


def icon_use(name, cx, cy, size, fill):
    vb = ICONS[name][0].split()
    w, h = float(vb[2]), float(vb[3])
    s = size / max(w, h)
    iw, ih = w * s, h * s
    return (f'<use href="#fa-{name}" x="{cx - iw / 2:.1f}" y="{cy - ih / 2:.1f}" '
            f'width="{iw:.1f}" height="{ih:.1f}" fill="{fill}"/>')


def render(nodes, edges, rankdir, title):
    ids = list(nodes)
    labels = {n: node_label(n, nodes[n]) for n in ids}
    if shutil.which("dot"):
        pos, geo, (W, H) = layout_graphviz(ids, labels, edges, rankdir)
        engine = "graphviz"
    else:
        pos, geo, (W, H) = layout_builtin(ids, labels, edges, rankdir)
        engine = "built-in"

    # Bounding box incl. labels.
    minx, miny, maxx, maxy = 0, 0, W, H
    for n, (x, y) in pos.items():
        half = text_w(labels[n], FONT) / 2 + 6
        minx, maxx = min(minx, x - half, x - R), max(maxx, x + half, x + R)
        miny, maxy = min(miny, y - R - 8), max(maxy, y + R + FONT + 12)
    for pts, lxy, _ in geo:
        for x, y in pts:
            minx, maxx, miny, maxy = min(minx, x), max(maxx, x), min(miny, y), max(maxy, y)
    pad = 30
    vx, vy, vw, vh = minx - pad, miny - pad, (maxx - minx) + 2 * pad, (maxy - miny) + 2 * pad

    used = {STYLE.get(node_kind(nodes[n]), DEFAULT_STYLE)[1] for n in ids} | {"skull", "gem"}
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}" '
         f'width="{vw:.0f}" height="{vh:.0f}" font-family="Helvetica, Arial, sans-serif">',
         f"<title>{escape(title)}</title>",
         "<defs>",
         '<marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7" markerHeight="7" '
         'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#5f6b7a"/></marker>']
    for name in sorted(used):
        vb, d = ICONS[name]
        o.append(f'<symbol id="fa-{name}" viewBox="{vb}"><path d="{d}"/></symbol>')
    o.append("</defs>")
    o.append(f'<rect x="{vx:.0f}" y="{vy:.0f}" width="{vw:.0f}" height="{vh:.0f}" fill="#ffffff"/>')

    # Edges
    o.append('<g fill="none" stroke="#5f6b7a" stroke-width="1.4">')
    for pts, _, _ in geo:
        o.append(f'<path d="{path_d(pts)}" marker-end="url(#arrow)"/>')
    o.append("</g>")
    o.append(f'<g font-size="{EFONT}" text-anchor="middle" fill="#2b2f36" stroke="#ffffff" '
             f'stroke-width="3" paint-order="stroke" stroke-linejoin="round">')
    for pts, lxy, lbl in geo:
        if not lbl:
            continue
        x, y = lxy if lxy else pts[len(pts) // 2]
        o.append(f'<text x="{x:.1f}" y="{y + EFONT / 3:.1f}">{escape(lbl)}</text>')
    o.append("</g>")

    # Nodes
    for n in ids:
        x, y = pos[n]
        kind = node_kind(nodes[n])
        fill, icon = STYLE.get(kind, DEFAULT_STYLE)
        owned, tier0 = node_flags(nodes[n])
        o.append(f'<g><title>{escape(labels[n])} ({escape(kind or "Unknown")})</title>')
        o.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{R}" fill="{fill}" stroke="{darker(fill)}" stroke-width="1.5"/>')
        o.append(icon_use(icon, x, y, ICON, "#1d1d1d"))
        if owned:
            bx, by = x + R * 0.72, y - R * 0.72
            o.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="8.5" fill="#d32f2f" stroke="#ffffff" stroke-width="1.5"/>')
            o.append(icon_use("skull", bx, by, 10, "#ffffff"))
        if tier0:
            bx, by = x - R * 0.72, y - R * 0.72
            o.append(f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="8.5" fill="#1f2a44" stroke="#ffffff" stroke-width="1.5"/>')
            o.append(icon_use("gem", bx, by, 10, "#ffffff"))
        o.append(f'<text x="{x:.1f}" y="{y + R + FONT + 3:.1f}" font-size="{FONT}" text-anchor="middle" '
                 f'fill="#1d1d1d" stroke="#ffffff" stroke-width="3" paint-order="stroke" '
                 f'stroke-linejoin="round">{escape(labels[n])}</text>')
        o.append("</g>")
    o.append("</svg>")
    return "\n".join(o), engine


def main():
    ap = argparse.ArgumentParser(description="Render a BloodHound CE graph JSON export as a BloodHound-styled SVG.")
    ap.add_argument("--json", required=True, type=Path, help="BloodHound CE graph JSON export (or /api/v2/graphs/cypher response)")
    ap.add_argument("--output", type=Path, help="output SVG file (default: input name with .svg)")
    ap.add_argument("--rankdir", default="LR", choices=["LR", "RL", "TB", "BT"], help="layout direction (default: LR)")
    args = ap.parse_args()

    if not args.json.is_file():
        sys.exit(f"[-] Input file not found: {args.json}")
    output = args.output or args.json.with_suffix(".svg")
    raw, nodes, edges = load(args.json)
    if not nodes:
        keys = list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
        sys.exit(f"[-] No nodes found in {args.json}. Top-level keys: {keys}")

    svg, engine = render(nodes, edges, args.rankdir, args.json.stem)
    output.write_text(svg, encoding="utf-8")
    print(f"[+] {len(nodes)} nodes, {len(edges)} edges ({engine} layout) -> {output}")


if __name__ == "__main__":
    main()
