Module warptap_sib {
    ScanInPort SI;
    SelectPort SEL;
    ScanOutPort SO { Source SR; }
    ScanInterface client { Port SI; Port SEL; Port SO; }

    ScanInPort fromSO;
    ScanOutPort toSI { Source SI; }
    // Structural (topology-level) select signal only -- see module docstring's
    // "real, deliberate scope boundary" note: does not re-encode
    // rtl/sib_cell.v's real po & shift_ff & select update-latch-commit timing.
    LogicSignal toSelSignal { SR & SEL; }
    ToSelectPort toSEL { Source toSelSignal; }
    ScanInterface host { Port fromSO; Port toSI; Port toSEL; }

    ScanRegister SR { ScanInSource SIBmux; CaptureSource SR; ResetValue 1'b0; }
    ScanMux SIBmux SelectedBy SR { 1'b0 : SI; 1'b1 : fromSO; }
}

Module warptap_instr_test_mode {
    // DataOutPort DO drives real host port bit(s): test_mode[0]
    DataOutPort DO[0:0] { Source DR[0:0]; }
    ScanInPort SI;
    ScanOutPort SO { Source SR; }

    ScanRegister SR[0:0] {
        ScanInSource SI;
        CaptureSource SR[0:0];  // self-capture: mirrors instrument_write.v's
                                   // own shift_ff <= po read-back-what-was-
                                   // last-committed behavior
        ResetValue 1'b0;
    }
    DataRegister DR[0:0] {
        WriteDataSource SR[0:0];
        WriteEnSource 1'b1;  // rtl/instrument_write.v commits unconditionally
                              // once selected -- see rtl/instrument_write.v's
                              // own module docstring for the real select gate.
        ResetValue 1'b0;
    }
}

Module warptap_instr_bist_start {
    // DataOutPort DO drives real host port bit(s): bist_start[0]
    DataOutPort DO[0:0] { Source DR[0:0]; }
    ScanInPort SI;
    ScanOutPort SO { Source SR; }

    ScanRegister SR[0:0] {
        ScanInSource SI;
        CaptureSource SR[0:0];  // self-capture: mirrors instrument_write.v's
                                   // own shift_ff <= po read-back-what-was-
                                   // last-committed behavior
        ResetValue 1'b0;
    }
    DataRegister DR[0:0] {
        WriteDataSource SR[0:0];
        WriteEnSource 1'b1;  // rtl/instrument_write.v commits unconditionally
                              // once selected -- see rtl/instrument_write.v's
                              // own module docstring for the real select gate.
        ResetValue 1'b0;
    }
}

Module warptap_instr_bist_done {
    ScanInPort SI;
    ScanOutPort SO { Source DR; }

    // CaptureSource fans out from real host port bit(s): bist_done[0]
    ScanRegister DR[0:0] {
        ScanInSource SI;
        CaptureSource DR[0:0];
        ResetValue 1'b0;
    }
}

Module warptap_instr_bist_fail {
    ScanInPort SI;
    ScanOutPort SO { Source DR; }

    // CaptureSource fans out from real host port bit(s): bist_fail[0]
    ScanRegister DR[0:0] {
        ScanInSource SI;
        CaptureSource DR[0:0];
        ResetValue 1'b0;
    }
}

Module input_demo_8x16_scn4m_mbist {
    ScanInPort tdi;
    ScanOutPort tdo { Source warptap_sib_bist_fail.SO; }
    TCKPort tck;
    TMSPort tms;
    TRSTPort trst_n;
    ScanInterface tap { Port tdi; Port tdo; Port tck; Port tms; Port trst_n; }

    Instance warptap_sib_test_mode Of warptap_sib { InputPort SI = tdi; InputPort fromSO = warptap_instr_test_mode.SO; }
    Instance warptap_instr_test_mode Of warptap_instr_test_mode { InputPort SI = warptap_sib_test_mode.toSI; }
    Instance warptap_sib_bist_start Of warptap_sib { InputPort SI = warptap_sib_test_mode.SO; InputPort fromSO = warptap_instr_bist_start.SO; }
    Instance warptap_instr_bist_start Of warptap_instr_bist_start { InputPort SI = warptap_sib_bist_start.toSI; }
    Instance warptap_sib_bist_done Of warptap_sib { InputPort SI = warptap_sib_bist_start.SO; InputPort fromSO = warptap_instr_bist_done.SO; }
    Instance warptap_instr_bist_done Of warptap_instr_bist_done { InputPort SI = warptap_sib_bist_done.toSI; }
    Instance warptap_sib_bist_fail Of warptap_sib { InputPort SI = warptap_sib_bist_done.SO; InputPort fromSO = warptap_instr_bist_fail.SO; }
    Instance warptap_instr_bist_fail Of warptap_instr_bist_fail { InputPort SI = warptap_sib_bist_fail.toSI; }
}
