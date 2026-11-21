Attribute VB_Name = "HighlightAgedBreaks"
' Sorts the Open_Breaks sheet by age (oldest first) and highlights
' breaks aged 5+ business days in red. Optional - the Python pipeline
' already colours the report; this is for re-applying it after edits.
Sub HighlightAgedBreaks()
    Dim ws As Worksheet, ageCol As Long, lastRow As Long, lastCol As Long, r As Long
    Set ws = ThisWorkbook.Worksheets("Open_Breaks")
    lastRow = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    lastCol = ws.Cells(1, ws.Columns.Count).End(xlToLeft).Column
    ageCol = Application.Match("age_bus_days", ws.Rows(1), 0)
    If lastRow < 2 Then Exit Sub

    With ws.Sort
        .SortFields.Clear
        .SortFields.Add Key:=ws.Cells(2, ageCol), Order:=xlDescending
        .SetRange ws.Range(ws.Cells(1, 1), ws.Cells(lastRow, lastCol))
        .Header = xlYes
        .Apply
    End With

    For r = 2 To lastRow
        If ws.Cells(r, ageCol).Value >= 5 Then
            ws.Range(ws.Cells(r, 1), ws.Cells(r, lastCol)).Interior.Color = RGB(255, 199, 206)
        End If
    Next r
End Sub
