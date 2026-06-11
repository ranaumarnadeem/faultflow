(* techmap_celltype = "\\$scanff_faultflow" *)
module faultflow_scanff_sky130_map (
    input CLK,
    input D,
    input SDI,
    input SE,
    output Q
);

    sky130_fd_sc_hd__sdfxtp_1 _TECHMAP_REPLACE_ (
        .CLK(CLK),
        .D(D),
        .SCD(SDI),
        .SCE(SE),
        .Q(Q)
    );

endmodule
