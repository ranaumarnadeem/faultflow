// The MBIST-insertion fixture chip: five instances of one memory macro, at
// different depths of the hierarchy.
//   u_core0.u_mem, u_core1.u_mem  one `core` module instantiated twice with the
//                                 same parameters, so configuring one forces a
//                                 module copy
//   g_bank[0].u_mem, g_bank[1].u_mem
//                                 a generate bank: paths with brackets
//   u_mem_top                     in the top module itself
// Every memory's read data is observable: through a register with an async
// reset, or straight at a chip output.
module chip_top (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [4:0] en,
    input  wire       we,
    input  wire [3:0] addr,
    input  wire [7:0] wdata,
    output wire [7:0] rdata_core0,
    output wire [7:0] rdata_core1,
    output wire [7:0] rdata_bank0,
    output wire [7:0] rdata_bank1,
    output reg  [7:0] rdata_top_q
);
    core u_core0 (
        .clk(clk), .rst_n(rst_n), .en(en[0]), .we(we), .addr(addr),
        .wdata(wdata), .rdata_q(rdata_core0)
    );
    core u_core1 (
        .clk(clk), .rst_n(rst_n), .en(en[1]), .we(we), .addr(addr),
        .wdata(~wdata), .rdata_q(rdata_core1)
    );

    wire [15:0] bank_dout;
    genvar i;
    generate
        for (i = 0; i < 2; i = i + 1) begin : g_bank
            input_demo_8x16_scn4m u_mem (
                .clk0  (clk),
                .csb0  (~en[2 + i]),
                .web0  (~we),
                .wmask0(1'b1),
                .addr0 (addr),
                .din0  (wdata),
                .dout0 (bank_dout[i*8 +: 8]),
                .clk1  (clk),
                .csb1  (1'b1),
                .addr1 (4'b0000),
                .dout1 ()
            );
        end
    endgenerate
    assign rdata_bank0 = bank_dout[7:0];
    assign rdata_bank1 = bank_dout[15:8];

    wire [7:0] top_dout;
    input_demo_8x16_scn4m u_mem_top (
        .clk0  (clk),
        .csb0  (~en[4]),
        .web0  (~we),
        .wmask0(1'b1),
        .addr0 (addr),
        .din0  (wdata ^ 8'hA5),
        .dout0 (top_dout),
        .clk1  (clk),
        .csb1  (1'b1),
        .addr1 (4'b0000),
        .dout1 ()
    );

    always @(posedge clk or negedge rst_n)
        if (!rst_n) rdata_top_q <= 8'h00;
        else        rdata_top_q <= top_dout;
endmodule
