#Requires AutoHotkey v2.0
#SingleInstance Force
CoordMode "Mouse", "Window"

; =====================================================================
;  PIS8 claim processing helper  (skeleton - calibrate before use)
;  F7 = show mouse position + window title (use this to fill in Pos)
;  F8 = run the batch        F9 = emergency stop
;  Verify each claim BY HAND first. This only does the repetitive part.
; =====================================================================

; ---------- CONFIG ----------
global DryRun     := true      ; true = does everything EXCEPT the final upload clicks
global ClaimCount := 3         ; how many rows to process this run (start small!)
global StepDelay  := 1000      ; ms pause after each click (raise if PIS is slow)
global PisTitle   := "PhilHealth Information System 8"
global LogFile    := A_ScriptDir "\pis_log.csv"
global Stop       := false

; ---------- CLICK POSITIONS (x, y relative to the PIS window) ----------
; Fill these in with F7. Names match the buttons in the Sub-Explorer.
Pos := Map(
    "FirstRow",        [0, 0],   ; first claim row in the grid
    "GenerateESOA",    [0, 0],
    "GenerateCF4",     [0, 0],
    "GenerateCF5",     [0, 0],
    "UploadDocuments", [0, 0],
    "UploadXML",       [0, 0],
    "ConfirmOK",       [0, 0]    ; the OK/Yes button on confirmation popups
)

; ---------- HOTKEYS ----------
F7:: {
    MouseGetPos &x, &y
    MsgBox "Window: " WinGetTitle("A") "`nX: " x "  Y: " y
}
F9:: {
    global Stop := true
}
F8:: RunBatch()

; ---------- MAIN ----------
RunBatch() {
    global Stop := false
    if !WinExist(PisTitle) {
        MsgBox "Open PIS and go to the claims grid first."
        return
    }
    WinActivate PisTitle
    WinWaitActive PisTitle, , 5

    Loop ClaimCount {
        if Stop
            break
        n := A_Index
        SelectRow(n)

        steps := [
            ["eSOA",             "GenerateESOA",    false],
            ["CF4",              "GenerateCF4",     false],
            ["CF5 XML",          "GenerateCF5",     false],
            ["Upload Documents", "UploadDocuments", true ],
            ["Upload XML",       "UploadXML",       true ]
        ]
        ok := true
        for s in steps {
            if Stop {
                ok := false
                break
            }
            name := s[1], key := s[2], isFinal := s[3]
            if (isFinal && DryRun) {
                Log(n, name, "SKIPPED (dry run)")
                continue
            }
            ClickPos(key)
            Sleep StepDelay
            if HasError() {
                Log(n, name, "ERROR dialog - stopped")
                ok := false
                break
            }
            ClickPos("ConfirmOK")   ; remove if a step has no popup
            Sleep StepDelay
            Log(n, name, "OK")
        }
        if !ok {
            MsgBox "Stopped on row " n ". Check PIS and pis_log.csv, then restart from there."
            return
        }
    }
    MsgBox "Done. Now run Get eClaims Uploaded / Status once by hand and review the log."
}

; ---------- HELPERS ----------
ClickPos(key) {
    global Pos
    p := Pos[key]
    Click p[1], p[2]
}

SelectRow(n) {
    ; Click the first row, then move down n-1 rows with the keyboard.
    ClickPos("FirstRow")
    Sleep 300
    if (n > 1)
        Send "{Down " (n - 1) "}"
    Sleep 300
}

HasError() {
    ; Adjust these titles to match the real error/warning popups in PIS.
    return WinExist("Error") || WinExist("Warning") || WinExist("Failed")
}

Log(claimNo, step, result) {
    global LogFile
    line := FormatTime(, "yyyy-MM-dd HH:mm:ss") "," claimNo "," step "," result "`n"
    FileAppend line, LogFile
}
