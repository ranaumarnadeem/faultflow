// soc1_ram.v -- SoC1's on-chip data/instruction RAM, extracted as its own
// wrapped block. Originally lived inline in soc1_glue.v, but assemble_soc's
// graybox EXTEST mode only drops *block* core logic, not the glue RTL's own
// content -- a bare RAM in the glue survives graybox composition as real,
// unfused flip-flops (8,192 for the 256-word array), which trips the native
// ATPG engine's combinational-only check during EXTEST. Making it a proper
// wrapped block fixes this: its FFs become part of a WBR-fused view like any
// other block, instead of leaking into the interconnect netlist untouched.
`default_nettype none

module soc1_ram #(
	parameter integer WORDS = 256
) (
	input  wire        clk,
	input  wire [ 3:0] wen,
	input  wire [21:0] addr,
	input  wire [31:0] wdata,
	output reg  [31:0] rdata
);
	reg [31:0] mem [0:WORDS-1];

	always @(posedge clk) begin
		rdata <= mem[addr];
		if (wen[0]) mem[addr][ 7: 0] <= wdata[ 7: 0];
		if (wen[1]) mem[addr][15: 8] <= wdata[15: 8];
		if (wen[2]) mem[addr][23:16] <= wdata[23:16];
		if (wen[3]) mem[addr][31:24] <= wdata[31:24];
	end
endmodule
