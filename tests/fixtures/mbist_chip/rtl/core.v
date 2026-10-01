// A core with one memory: port 0 does the work, port 1 is unused -- its
// select tied off, its address tied to 0, its clock shared with port 0 and its
// read data left unread.
module core (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       en,
    input  wire       we,
    input  wire [3:0] addr,
    input  wire [7:0] wdata,
    output reg  [7:0] rdata_q
);
    wire [7:0] dout;
    wire [7:0] dout_port1;

    input_demo_8x16_scn4m u_mem (
        .clk0  (clk),
        .csb0  (~en),
        .web0  (~we),
        .wmask0(1'b1),
        .addr0 (addr),
        .din0  (wdata),
        .dout0 (dout),
        .clk1  (clk),
        .csb1  (1'b1),
        .addr1 (4'b0000),
        .dout1 (dout_port1)
    );

    always @(posedge clk or negedge rst_n)
        if (!rst_n) rdata_q <= 8'h00;
        else        rdata_q <= dout;
endmodule
