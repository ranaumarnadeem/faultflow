// ============================================================================
// Flattened from HarieshAnbalagan/RV32I (multi_cycle_processor), MIT licensed.
// Mechanically concatenated + type-qualified for faultflow's
// single-read_verilog-invocation synthesis flow -- see
// tests/python/test_synth_systemverilog.py and CLAUDE.md's '-sv note'
// for why package typedef'd types/enum literals are referenced as
// `risc_v_32_i_pkg::NAME` (wildcard import alone doesn't resolve them
// in this Yosys version, though it's kept -- it correctly resolves
// bare package parameters like XLEN). No logic changed.
// ============================================================================

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Package: risc_v_32_i_pkg.sv
*
* Description:
*
* Contains the parameters and enums required.
***************************************************************************/
package risc_v_32_i_pkg;

parameter XLEN = 32;
parameter ADDR_WIDTH = 32;
parameter REG_ADDR_WIDTH = 5;

parameter IMM_SEL_LEN = 3;

typedef enum logic[IMM_SEL_LEN-1:0] {
    IMM_B_TYPE,
    IMM_S_TYPE,
    IMM_U_TYPE,
    IMM_I_TYPE,
    IMM_J_TYPE,
    IMM_UNKNOWN_TYPE
} imm_select_e;

parameter ALU_SEL_LEN = 4;

typedef enum logic [ALU_SEL_LEN-1:0] {
    OP_ADD,
    OP_SUB,
    OP_AND,
    OP_OR,
    OP_XOR,
    OP_SLL,
    OP_SRL,
    OP_SRA,
    OP_UNKNOWN
} alu_select_e;

parameter COMP_SEL_LEN = 3;

typedef enum logic [COMP_SEL_LEN-1:0] {
    OP_BEQ,
    OP_BNE,
    OP_BLT,
    OP_BGE,
    OP_BLTU,
    OP_BGEU,
    OP_BUNKNOWN
} comp_select_e;

parameter RD_MUX_SEL_LEN = 3;

typedef enum logic [RD_MUX_SEL_LEN-1:0] {
    RD_MUX_DMEM,
    RD_MUX_ALU,
    RD_MUX_BCU,
    RD_MUX_IMM,
    RD_MUX_PC_N,
    RD_MUX_N_A
}write_data_select_e;

parameter LOAD_STORE_TYPE_LEN = 4;

typedef enum logic [LOAD_STORE_TYPE_LEN-1:0]
{
    L_W,
    L_H,
    L_HU,
    L_B,
    L_BU,
    S_W,
    S_H,
    S_B,
    LS_N_A
}load_store_type_e;

endpackage

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: RegisterFile.sv
*
* Description:
*
* This holds the architectural register file, which contains the registers
* associated with processor to store data
***************************************************************************/

module RegisterFile #(parameter XLEN = 32, parameter REG_ADDR_WIDTH = 5)
(
    output logic [XLEN-1:0]             read_data_1_o,
    output logic [XLEN-1:0]             read_data_2_o,
    input  logic                        clk_i,
    input  logic                        write_enable_i,
    input  logic [XLEN-1:0]             write_data_i,
    input  logic [REG_ADDR_WIDTH-1:0]   write_address_i,
    input  logic [REG_ADDR_WIDTH-1:0]   read_address_1_i,
    input  logic [REG_ADDR_WIDTH-1:0]   read_address_2_i
);

    logic [XLEN-1:0]register [XLEN-1:0];

    assign read_data_1_o = (read_address_1_i == {REG_ADDR_WIDTH{1'b0}}) ? 0 : register[read_address_1_i];
    assign read_data_2_o = (read_address_2_i == {REG_ADDR_WIDTH{1'b0}}) ? 0 : register[read_address_2_i];

    always_ff @(negedge clk_i)
    begin
        if (write_enable_i == 1 && write_address_i != {REG_ADDR_WIDTH{1'b0}})
        begin
            register[write_address_i] <= write_data_i;
        end
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ImmediateSignExtend.sv
*
* Description:
*
* This sign extends the immediate value from instructions, not applicable
* for R-type instruction as it uses data from RegistreFile instead.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ImmediateSignExtend #(parameter XLEN = 32)
(
    output logic        [XLEN-1:0]  imm_o,
    input  logic        [XLEN-1:7]  imm_i,
    input  risc_v_32_i_pkg::imm_select_e             imm_sel_i
);

    always_comb
    begin
        unique case(imm_sel_i)
            risc_v_32_i_pkg::IMM_B_TYPE: imm_o = $signed({imm_i[31], imm_i[7], imm_i[30:25], imm_i[11:8], 1'b0});
            risc_v_32_i_pkg::IMM_S_TYPE: imm_o = $signed({imm_i[31:25], imm_i[11:7]});
            risc_v_32_i_pkg::IMM_U_TYPE: imm_o = {imm_i[31:12], 12'b0};
            risc_v_32_i_pkg::IMM_I_TYPE: imm_o = $signed({imm_i[31:20]});
            risc_v_32_i_pkg::IMM_J_TYPE: imm_o = $signed({imm_i[31], imm_i[19:12], imm_i[20], imm_i[30:21], 1'b0});
            risc_v_32_i_pkg::IMM_UNKNOWN_TYPE: imm_o = 0;
            default: imm_o = 0;
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ArithmeticLogicUnit.sv
*
* Description:
*
* This does computation over the data related to arithmetic and logic
* instructions. This does not holds the comparison which is done by
* ComparatorUnit.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ArithmeticLogicUnit #(parameter XLEN = 32, parameter REG_ADDR_WIDTH = 5)
(
    output logic        [XLEN-1:0]  alu_o,
    input  logic        [XLEN-1:0]  alu_port_a_i,
    input  logic        [XLEN-1:0]  alu_port_b_i,
    input  risc_v_32_i_pkg::alu_select_e             alu_op_sel_i
);

    always_comb
    begin
        unique case(alu_op_sel_i)
            risc_v_32_i_pkg::OP_ADD:     alu_o =         alu_port_a_i  +   alu_port_b_i;
            risc_v_32_i_pkg::OP_SUB:     alu_o =         alu_port_a_i  -   alu_port_b_i;
            risc_v_32_i_pkg::OP_AND:     alu_o =         alu_port_a_i  &   alu_port_b_i;
             risc_v_32_i_pkg::OP_OR:     alu_o =         alu_port_a_i  |   alu_port_b_i;
            risc_v_32_i_pkg::OP_XOR:     alu_o =         alu_port_a_i  ^   alu_port_b_i;
            risc_v_32_i_pkg::OP_SLL:     alu_o =         alu_port_a_i  <<  alu_port_b_i[REG_ADDR_WIDTH-1:0];
            risc_v_32_i_pkg::OP_SRL:     alu_o =         alu_port_a_i  >>  alu_port_b_i[REG_ADDR_WIDTH-1:0];
            risc_v_32_i_pkg::OP_SRA:     alu_o = $signed(alu_port_a_i) >>> alu_port_b_i[REG_ADDR_WIDTH-1:0];
        risc_v_32_i_pkg::OP_UNKNOWN:     alu_o = {XLEN{1'b0}};
           default:      alu_o = {XLEN{1'b0}};
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ComparatorUnit.sv
*
* Description:
*
* This does computation over the data related to comparison operations.
* The final output is a singe bit. This is kept seperatly from ALU such that
* ALU does addition and this unit does comparision at same time to avoid 
* seperate adder for branch operation.
***************************************************************************/
import risc_v_32_i_pkg::*;

module ComparatorUnit #(parameter XLEN = 32)
(
    output logic                        comp_o,
    input  logic           [XLEN-1:0]   comp_port_a_i,
    input  logic           [XLEN-1:0]   comp_port_b_i,
    input  risc_v_32_i_pkg::comp_select_e                comp_op_sel_i
);

    always_comb
    begin
        unique case(comp_op_sel_i)
            risc_v_32_i_pkg::OP_BEQ:     comp_o =         comp_port_a_i  ==  comp_port_b_i;
            risc_v_32_i_pkg::OP_BNE:     comp_o =         comp_port_a_i  !=  comp_port_b_i;
           risc_v_32_i_pkg::OP_BLTU:     comp_o =         comp_port_a_i  <   comp_port_b_i;
           risc_v_32_i_pkg::OP_BGEU:     comp_o =         comp_port_a_i  >=  comp_port_b_i;
            risc_v_32_i_pkg::OP_BLT:     comp_o = $signed(comp_port_a_i) <   $signed(comp_port_b_i);
            risc_v_32_i_pkg::OP_BGE:     comp_o = $signed(comp_port_a_i) >=  $signed(comp_port_b_i);
       risc_v_32_i_pkg::OP_BUNKNOWN:     comp_o = 1'b0;
          default:      comp_o = 1'b0;
        endcase
    end
endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: LoadAndStoreUnit.sv
*
* Description:
*
* This module handles the load and store opertaion between processor
* and memeory elements. This is not complete yet to be developed fully.
***************************************************************************/
import risc_v_32_i_pkg::*;

module LoadAndStoreUnit #(parameter XLEN = 32)
(
    output logic [XLEN-1:0]     read_data_o,
    input  logic [XLEN-1:0]     read_data_i,
    output logic [XLEN-1:0]     write_data_o,
    input  logic [XLEN-1:0]     write_data_i,
    input risc_v_32_i_pkg::load_store_type_e     load_store_type_i,
    output logic [3:0]          write_data_strobe_o
);

    always_comb
    begin
        unique case(load_store_type_i)
               risc_v_32_i_pkg::L_B: read_data_o =               $signed(read_data_i[(XLEN/4)-1:0]);
              risc_v_32_i_pkg::L_BU: read_data_o = {{((3*XLEN)/4){1'b0}},read_data_i[(XLEN/4)-1:0]};
               risc_v_32_i_pkg::L_H: read_data_o =               $signed(read_data_i[(XLEN/2)-1:0]);
              risc_v_32_i_pkg::L_HU: read_data_o = {{(( XLEN /2)){1'b0}},read_data_i[(XLEN/2)-1:0]};
               risc_v_32_i_pkg::L_W: read_data_o =                       read_data_i[XLEN-1:0];
           default: read_data_o =                       read_data_i[XLEN-1:0];
        endcase
    end

    always_comb
    begin
        unique case(load_store_type_i)
               risc_v_32_i_pkg::S_B:
                   begin
                   write_data_o = {{((3*XLEN)/4){1'b0}},write_data_i[(XLEN/4)-1:0]};
                   write_data_strobe_o = 4'b0001;
                   end
               risc_v_32_i_pkg::S_H:
                   begin
                   write_data_o = {{(( XLEN /2)){1'b0}},write_data_i[(XLEN/2)-1:0]};
                   write_data_strobe_o = 4'b0011;
                   end
               risc_v_32_i_pkg::S_W:
                   begin
                   write_data_o = write_data_i[XLEN-1:0];
                   write_data_strobe_o = 4'b1111;
                   end
           default:
                   begin
                   write_data_o = write_data_i[XLEN-1:0];
                   write_data_strobe_o = 4'b0000;
                   end
        endcase
    end

endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: CoreControlUnit.sv
*
* Description:
*
* This does control of the processor core datapath. This does not cover
* the pipeline control which is hanleded by PipelineControlUnit.
***************************************************************************/
import risc_v_32_i_pkg::*;

module CoreControlUnit #(parameter XLEN = 32)
(
    output logic [1:0]              pc_mux_sel_o,
    output logic                    reg_write_enable_o,
    output risc_v_32_i_pkg::imm_select_e             imm_select_o,
    output logic                    execute_port_a_sel_o,
    output logic                    execute_port_b_sel_o,
    output risc_v_32_i_pkg::alu_select_e             alu_op_sel_o,
    output risc_v_32_i_pkg::comp_select_e            comp_op_sel_o,
    output risc_v_32_i_pkg::load_store_type_e        load_store_type_o,
    output logic                    data_memory_write_enable_o,
    output risc_v_32_i_pkg::write_data_select_e      reg_write_data_sel_o,
    input logic [6:0]               op_code_i,
    input logic [2:0]               funct3_i,
    input logic                     funct7_bit5_i
);

    localparam OP_R_TYPE        = 7'b0110011;
    localparam OP_B_TYPE        = 7'b1100011;
    localparam OP_S_TYPE        = 7'b0100011;
    localparam OP_I_JALR_TYPE   = 7'b1100111;
    localparam OP_I_LOAD_TYPE   = 7'b0000011;
    localparam OP_I_ALU_TYPE    = 7'b0010011;
    localparam OP_I_FENCE_TYPE  = 7'b0001111;
    localparam OP_I_ECALL_TYPE  = 7'b1110011;
    localparam OP_U_LUI_TYPE    = 7'b0110111;
    localparam OP_U_AUIPC_TYPE  = 7'b0010111;
    localparam OP_J_TYPE        = 7'b1101111;

    always_comb
    begin

        unique case(op_code_i)
            OP_R_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_UNKNOWN_TYPE;
                execute_port_a_sel_o = 1'b1;

                unique case({funct7_bit5_i,funct3_i})
                    4'b0000:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b1000:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SUB;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0001:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SLL;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0010:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                            execute_port_b_sel_o = 1'b0;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                            end
                    4'b0011:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                            execute_port_b_sel_o = 1'b0;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                            end
                    4'b0100:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_XOR;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0101:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SRL;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b1101:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_SRA;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0110:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_OR;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    4'b0111:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_AND;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                    default:
                            begin
                            alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                            comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                            execute_port_b_sel_o = 1'b1;
                            reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                            end
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
            end
            OP_B_TYPE:
            begin
                pc_mux_sel_o = 2'b10;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_B_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;

                unique case(funct3_i)
                    3'b000: comp_op_sel_o = risc_v_32_i_pkg::OP_BEQ;
                    3'b001: comp_op_sel_o = risc_v_32_i_pkg::OP_BNE;
                    3'b100: comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                    3'b101: comp_op_sel_o = risc_v_32_i_pkg::OP_BGE;
                    3'b110: comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                    3'b111: comp_op_sel_o = risc_v_32_i_pkg::OP_BGEU;
                   default: comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_S_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_S_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;

                unique case(funct3_i)
                    3'b000:  load_store_type_o = risc_v_32_i_pkg::S_B;
                    3'b001:  load_store_type_o = risc_v_32_i_pkg::S_H;
                    3'b010:  load_store_type_o = risc_v_32_i_pkg::S_W;
                endcase

                data_memory_write_enable_o = 1'b1;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_I_JALR_TYPE:
            begin
                pc_mux_sel_o = 2'b01;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_PC_N;
            end
            OP_I_LOAD_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;

                unique case(funct3_i)
                    3'b000:  load_store_type_o = risc_v_32_i_pkg::L_B;
                    3'b001:  load_store_type_o = risc_v_32_i_pkg::L_H;
                    3'b010:  load_store_type_o = risc_v_32_i_pkg::L_W;
                    3'b100:  load_store_type_o = risc_v_32_i_pkg::L_BU;
                    3'b101:  load_store_type_o = risc_v_32_i_pkg::L_HU;
                    default: load_store_type_o = risc_v_32_i_pkg::L_W;
                endcase

                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_DMEM;
            end
            OP_I_ALU_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;

                unique case(funct3_i)
                    3'b000:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b010:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BLT;
                           execute_port_b_sel_o = 1'b1;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                           end
                    3'b011:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BLTU;
                           execute_port_b_sel_o = 1'b1;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_BCU;
                           end
                    3'b100:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_XOR;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b110:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_OR;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b111:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_AND;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b001:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_SLL;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                    3'b101:
                           begin
                           alu_op_sel_o = ((funct7_bit5_i)? risc_v_32_i_pkg::OP_SRA : risc_v_32_i_pkg::OP_SRL);
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                   default:
                           begin
                           alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                           comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                           execute_port_b_sel_o = 1'b0;
                           reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
                           end
                endcase

                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
            end
            OP_I_FENCE_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_I_ECALL_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_I_TYPE;
                execute_port_a_sel_o = 1'b1;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
            OP_U_LUI_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_U_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_IMM;
            end
            OP_U_AUIPC_TYPE:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_U_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_ALU;
            end
            OP_J_TYPE:
            begin
                pc_mux_sel_o = 2'b01;
                reg_write_enable_o = 1'b1;
                imm_select_o = risc_v_32_i_pkg::IMM_J_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_ADD;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_PC_N;
            end
            default:
            begin
                pc_mux_sel_o = 2'b00;
                reg_write_enable_o = 1'b0;
                imm_select_o = risc_v_32_i_pkg::IMM_UNKNOWN_TYPE;
                execute_port_a_sel_o = 1'b0;
                execute_port_b_sel_o = 1'b0;
                alu_op_sel_o = risc_v_32_i_pkg::OP_UNKNOWN;
                comp_op_sel_o = risc_v_32_i_pkg::OP_BUNKNOWN;
                load_store_type_o = risc_v_32_i_pkg::LS_N_A;
                data_memory_write_enable_o = 1'b0;
                reg_write_data_sel_o = risc_v_32_i_pkg::RD_MUX_N_A;
            end
        endcase

    end
endmodule

/***************************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: PipelineControlUnit.sv
*
* Description:
*
* This does control of the processor pipeline
***************************************************************************/
import risc_v_32_i_pkg::*;

module PipelineControlUnit #(parameter REG_ADDR_WIDTH = 5)
(
    output logic                        stall_pc_if_o,
    output logic                        stall_if_id_o,
    output logic                        clear_if_id_o,
    output logic                        clear_id_ex_o,
    output logic [1:0]                  reg_source_1_data_sel_o,
    output logic [1:0]                  reg_source_2_data_sel_o,
     input logic [REG_ADDR_WIDTH-1:0]   reg_source_1_addr_id_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_source_2_addr_id_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_source_1_addr_ex_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_source_2_addr_ex_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_destination_addr_ex_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_destination_addr_dm_i,
     input logic [REG_ADDR_WIDTH-1:0]   reg_destination_addr_wb_i,
     input logic                        reg_write_enable_dm_i,
     input logic                        reg_write_enable_wb_i,
     input risc_v_32_i_pkg::write_data_select_e          reg_write_data_sel_ex_i,
     input logic                        branch_enable_i
);

    logic load_stall;

    assign reg_source_1_data_sel_o[0] = ((reg_source_1_addr_ex_i != 32'd0) && (reg_source_1_addr_ex_i == reg_destination_addr_dm_i) && (reg_write_enable_dm_i));
    assign reg_source_1_data_sel_o[1] = ((reg_source_1_addr_ex_i != 32'd0) && (reg_source_1_addr_ex_i == reg_destination_addr_wb_i) && (reg_write_enable_wb_i));
    assign reg_source_2_data_sel_o[0] = ((reg_source_2_addr_ex_i != 32'd0) && (reg_source_2_addr_ex_i == reg_destination_addr_dm_i) && (reg_write_enable_dm_i));
    assign reg_source_2_data_sel_o[1] = ((reg_source_2_addr_ex_i != 32'd0) && (reg_source_2_addr_ex_i == reg_destination_addr_wb_i) && (reg_write_enable_wb_i));
    assign load_stall = ((reg_write_data_sel_ex_i == risc_v_32_i_pkg::RD_MUX_DMEM) && ((reg_source_1_addr_id_i == reg_destination_addr_ex_i) || (reg_source_2_addr_id_i == reg_destination_addr_ex_i)));
    assign stall_if_id_o = load_stall;
    assign stall_pc_if_o = load_stall;
    assign clear_if_id_o = branch_enable_i;
    assign clear_id_ex_o = (branch_enable_i | load_stall);

endmodule

/***************************************************************
* Copyright (c) 2022 Hariesh Anbalagan
* This code is licensed under MIT license (see LICENSE.txt for details)
* 
* Module: ProcessorCore.sv
*
* Description:
*
* This does contains to processor core datapath.
* (Load and store are yet to developed fully)
***************************************************************/
import risc_v_32_i_pkg::*;

module ProcessorCore
(
    input logic             clk_i,
    input logic             reset_i,

    output logic [31:0]     instruction_address_o,
     input logic [31:0]     instruction_data_i,

     input logic [31:0]     read_data_i,
    output logic            write_enable_o,
    output logic [31:0]     write_data_o,
    output logic [3:0]      write_data_strobe_o,
    output logic [31:0]     address_o
);

///////////////////////////////////////////////////////////////
//                 INSTRUCTION FETCH SIGNALS                 //
///////////////////////////////////////////////////////////////

    logic [ADDR_WIDTH-1:0]PC_if;
    logic [ADDR_WIDTH-1:0]pc_next_if;
    logic [ADDR_WIDTH-1:0]pc_update_if;
    logic [XLEN-1:0]instruction_if;

///////////////////////////////////////////////////////////////
//                 INSTRUCTION DECODE SIGNALS                //
///////////////////////////////////////////////////////////////

    logic [ADDR_WIDTH-1:0]PC_id;
    logic [ADDR_WIDTH-1:0]pc_next_id;
    logic [XLEN-1:0]instruction_id;
    logic [REG_ADDR_WIDTH-1:0]reg_source_1_addr_id;
    logic [REG_ADDR_WIDTH-1:0]reg_source_2_addr_id;
    logic [REG_ADDR_WIDTH-1:0]reg_destination_addr_id;
    logic [XLEN-1:0]immediate_id;
    logic [XLEN-1:0]reg_source_1_data_id;
    logic [XLEN-1:0]reg_source_2_data_id;
    risc_v_32_i_pkg::load_store_type_e load_store_type_id;
    logic [1:0]pc_mux_sel_id;
    logic reg_write_enable_id;
    risc_v_32_i_pkg::imm_select_e imm_select_id;
    logic execute_port_a_sel_id;
    logic execute_port_b_sel_id;
    risc_v_32_i_pkg::alu_select_e alu_op_sel_id;
    risc_v_32_i_pkg::comp_select_e comp_op_sel_id;
    logic data_memory_write_enable_id;
    risc_v_32_i_pkg::write_data_select_e reg_write_data_sel_id;

///////////////////////////////////////////////////////////////
//                      EXECUTE SIGNALS                      //
///////////////////////////////////////////////////////////////

    logic [ADDR_WIDTH-1:0]PC_ex;
    logic [ADDR_WIDTH-1:0]pc_next_ex;
    logic [REG_ADDR_WIDTH-1:0]reg_source_1_addr_ex;
    logic [REG_ADDR_WIDTH-1:0]reg_source_2_addr_ex;
    logic [REG_ADDR_WIDTH-1:0]reg_destination_addr_ex;
    logic [XLEN-1:0]immediate_ex;
    logic [XLEN-1:0]reg_source_1_data_ex;
    logic [XLEN-1:0]reg_source_2_data_ex;
    logic [XLEN-1:0]alu_port_a_ex;
    logic [XLEN-1:0]alu_port_b_ex;
    logic [XLEN-1:0]comp_port_a_ex;
    logic [XLEN-1:0]comp_port_b_ex;
    logic [XLEN-1:0]alu_output_ex;
    logic comp_output_ex;
    risc_v_32_i_pkg::load_store_type_e load_store_type_ex;
    logic [1:0]pc_mux_sel_ex;
    logic reg_write_enable_ex;
    risc_v_32_i_pkg::imm_select_e imm_select_ex;
    logic execute_port_a_sel_ex;
    logic execute_port_b_sel_ex;
    risc_v_32_i_pkg::alu_select_e alu_op_sel_ex;
    risc_v_32_i_pkg::comp_select_e comp_op_sel_ex;
    logic data_memory_write_enable_ex;
    risc_v_32_i_pkg::write_data_select_e reg_write_data_sel_ex;

///////////////////////////////////////////////////////////////
//                 DATA MEMORY SIGNALS                       //
///////////////////////////////////////////////////////////////

    logic [ADDR_WIDTH-1:0]pc_next_dm;
    logic [REG_ADDR_WIDTH-1:0]reg_destination_addr_dm;
    logic [XLEN-1:0]immediate_dm;
    logic [XLEN-1:0]reg_source_2_data_dm;
    logic [XLEN-1:0]alu_output_dm;
    logic comp_output_dm;
    logic [XLEN-1:0]read_data_aligned_dm;
    logic [XLEN-1:0]write_data_unaligned_dm;
    risc_v_32_i_pkg::load_store_type_e load_store_type_dm;
    logic [XLEN-1:0]dmem_data_dm;
    logic [XLEN-1:0]reg_data_dm;
    logic reg_write_enable_dm;
    risc_v_32_i_pkg::imm_select_e imm_select_dm;
    logic data_memory_write_enable_dm;
    risc_v_32_i_pkg::write_data_select_e reg_write_data_sel_dm;

///////////////////////////////////////////////////////////////
//                 WRITE BACK SIGNALS                        //
///////////////////////////////////////////////////////////////

    logic [ADDR_WIDTH-1:0]pc_next_wb;
    logic [REG_ADDR_WIDTH-1:0]reg_destination_addr_wb;
    logic [XLEN-1:0]immediate_wb;
    logic [XLEN-1:0]alu_output_wb;
    logic comp_output_wb;
    logic [XLEN-1:0]dmem_data_wb;
    logic reg_write_enable_wb;
    risc_v_32_i_pkg::imm_select_e imm_select_wb;
    risc_v_32_i_pkg::write_data_select_e reg_write_data_sel_wb;
    logic [XLEN-1:0]reg_data_wb;

///////////////////////////////////////////////////////////////
//           PIPELINE DATA / CONTROL SIGNALS                 //
///////////////////////////////////////////////////////////////

    logic stall_pc_if;
    logic stall_if_id;
    logic clear_if_id;
    logic clear_id_ex;
    logic branch_enable;
    logic [XLEN-1:0] reg_source_1_data;
    logic [XLEN-1:0] reg_source_2_data;
    logic [1:0]reg_source_1_data_sel;
    logic [1:0]reg_source_2_data_sel;
    logic [XLEN-1:0]reg_data;

///////////////////////////////////////////////////////////////
//                 INSTRUCTION FETCH STAGE                   //
///////////////////////////////////////////////////////////////

    assign pc_next_if = PC_if + {{XLEN-3{1'b0}},3'b100};
    always_comb
    begin
        if((pc_mux_sel_ex[0] == 1'b1) || ((pc_mux_sel_ex[1] == 1'b1) && (comp_output_ex == 1'b1)))
        begin
            branch_enable = 1'b1;
        end
        else
        begin
            branch_enable = 1'b0;
        end
    end
    
    assign pc_update_if = branch_enable ? alu_output_ex : pc_next_if;
    
    always_ff @(posedge clk_i, posedge reset_i)
    begin
        if (reset_i)
        begin
            PC_if <= 32'd0;
        end
        else if(~stall_pc_if)
        begin
            PC_if <= pc_update_if;
        end
    end

    assign instruction_address_o = PC_if;
    assign instruction_if = instruction_data_i;

///////////////////////////////////////////////////////////////
//    INSTRUCTION FETCH / INSTRUCTION DECODE INTERFACE       //
///////////////////////////////////////////////////////////////

    always_ff @(posedge clk_i)
    begin
        if (clear_if_id | reset_i)
        begin
            PC_id <= {ADDR_WIDTH{1'b0}};
            pc_next_id <= {ADDR_WIDTH{1'b0}};
            instruction_id <= {XLEN{1'b0}};
            reg_source_1_addr_id <= {REG_ADDR_WIDTH{1'b0}};
            reg_source_2_addr_id <= {REG_ADDR_WIDTH{1'b0}};
            reg_destination_addr_id <= {REG_ADDR_WIDTH{1'b0}};
        end
        else if(~stall_if_id)
        begin
            PC_id <= PC_if;
            pc_next_id <= pc_next_if;
            instruction_id <= instruction_if;
            reg_source_1_addr_id <= instruction_if[19:15];
            reg_source_2_addr_id <= instruction_if[24:20];
            reg_destination_addr_id <= instruction_if[11:7];
        end
        else
        begin
            PC_id <= PC_id;
            pc_next_id <= pc_next_id;
            instruction_id <= instruction_id;
            reg_source_1_addr_id <= reg_source_1_addr_id;
            reg_source_2_addr_id <= reg_source_2_addr_id;
            reg_destination_addr_id <= reg_destination_addr_id;
        end
    end

///////////////////////////////////////////////////////////////
//                INSTRUCTION DECODE STAGE                   //
///////////////////////////////////////////////////////////////

    RegisterFile #(.XLEN(XLEN), .REG_ADDR_WIDTH(REG_ADDR_WIDTH)) regfile
    (
        .read_data_1_o      (reg_source_1_data_id),
        .read_data_2_o      (reg_source_2_data_id),
        .clk_i              (clk_i),
        .write_enable_i     (reg_write_enable_wb),
        .write_data_i       (reg_data_wb),
        .write_address_i    (reg_destination_addr_wb),
        .read_address_1_i   (reg_source_1_addr_id),
        .read_address_2_i   (reg_source_2_addr_id)
    );

    ImmediateSignExtend #(.XLEN(XLEN)) ise
    (
        .imm_o      (immediate_id),
        .imm_i      (instruction_id[31:7]),
        .imm_sel_i  (imm_select_id)
    );

    CoreControlUnit ccu
    (
        .pc_mux_sel_o                     (pc_mux_sel_id),
        .reg_write_enable_o               (reg_write_enable_id),
        .imm_select_o                     (imm_select_id),
        .execute_port_a_sel_o             (execute_port_a_sel_id),
        .execute_port_b_sel_o             (execute_port_b_sel_id),
        .alu_op_sel_o                     (alu_op_sel_id),
        .comp_op_sel_o                    (comp_op_sel_id),
        .load_store_type_o                (load_store_type_id),
        .data_memory_write_enable_o       (data_memory_write_enable_id),
        .reg_write_data_sel_o             (reg_write_data_sel_id),
        .op_code_i                        (instruction_id[6:0]),
        .funct3_i                         (instruction_id[14:12]),
        .funct7_bit5_i                    (instruction_id[30])
    );

///////////////////////////////////////////////////////////////
//         INSTRUCTION DECODE / EXECUTE INTERFACE            //
///////////////////////////////////////////////////////////////

    always_ff @(posedge clk_i)
    begin
        if (clear_id_ex | reset_i)
        begin
            PC_ex <= {ADDR_WIDTH{1'b0}};
            pc_next_ex <= {ADDR_WIDTH{1'b0}};
            reg_source_1_addr_ex <= {REG_ADDR_WIDTH{1'b0}};
            reg_source_2_addr_ex <= {REG_ADDR_WIDTH{1'b0}};
            reg_destination_addr_ex <= {REG_ADDR_WIDTH{1'b0}};
            immediate_ex <= {XLEN{1'b0}};
            reg_source_1_data_ex <= {XLEN{1'b0}};
            reg_source_2_data_ex <= {XLEN{1'b0}};
            load_store_type_ex <= risc_v_32_i_pkg::LS_N_A;
            pc_mux_sel_ex <= 2'b00;
            reg_write_enable_ex <= 1'b0;
            imm_select_ex <= risc_v_32_i_pkg::IMM_UNKNOWN_TYPE;
            execute_port_a_sel_ex <= 1'b1;
            execute_port_b_sel_ex <= 1'b1;
            alu_op_sel_ex <= risc_v_32_i_pkg::OP_UNKNOWN;
            comp_op_sel_ex <= risc_v_32_i_pkg::OP_BUNKNOWN;
            data_memory_write_enable_ex <= {1'b0};
            reg_write_data_sel_ex <= risc_v_32_i_pkg::RD_MUX_ALU;
        end
        else if(~stall_if_id)
        begin
            PC_ex <= PC_id;
            pc_next_ex <= pc_next_id;
            reg_source_1_addr_ex <= reg_source_1_addr_id;
            reg_source_2_addr_ex <= reg_source_2_addr_id;
            reg_destination_addr_ex <= reg_destination_addr_id;
            immediate_ex <= immediate_id;
            reg_source_1_data_ex <= reg_source_1_data_id;
            reg_source_2_data_ex <= reg_source_2_data_id;
            load_store_type_ex <= load_store_type_id;
            pc_mux_sel_ex <= pc_mux_sel_id;
            reg_write_enable_ex <= reg_write_enable_id;
            imm_select_ex <= imm_select_id;
            execute_port_a_sel_ex <= execute_port_a_sel_id;
            execute_port_b_sel_ex <= execute_port_b_sel_id;
            alu_op_sel_ex <= alu_op_sel_id;
            comp_op_sel_ex <= comp_op_sel_id;
            data_memory_write_enable_ex <= data_memory_write_enable_id;
            reg_write_data_sel_ex <= reg_write_data_sel_id;
        end
        else
        begin
            PC_ex <= PC_ex;
            pc_next_ex <= pc_next_ex;
            reg_source_1_addr_ex <= reg_source_1_addr_ex;
            reg_source_2_addr_ex <= reg_source_2_addr_ex;
            reg_destination_addr_ex <= reg_destination_addr_ex;
            immediate_ex <= immediate_ex;
            reg_source_1_data_ex <= reg_source_1_data_ex;
            reg_source_2_data_ex <= reg_source_2_data_ex;
            load_store_type_ex <= load_store_type_ex;
            pc_mux_sel_ex <= pc_mux_sel_ex;
            reg_write_enable_ex <= reg_write_enable_ex;
            imm_select_ex <= imm_select_ex;
            execute_port_a_sel_ex <= execute_port_a_sel_ex;
            execute_port_b_sel_ex <= execute_port_b_sel_ex;
            alu_op_sel_ex <= alu_op_sel_ex;
            comp_op_sel_ex <= comp_op_sel_ex;
            data_memory_write_enable_ex <= data_memory_write_enable_ex;
            reg_write_data_sel_ex <= reg_write_data_sel_ex;
        end
    end

///////////////////////////////////////////////////////////////
//                     EXECUTE STAGE                         //
///////////////////////////////////////////////////////////////
    always_comb
    begin
        case(reg_source_1_data_sel)
            2'b10: reg_source_1_data = reg_data_wb;
            2'b01: reg_source_1_data = reg_data_dm;
            2'b00: reg_source_1_data = reg_source_1_data_ex;
            default: reg_source_1_data = reg_source_1_data_ex;
        endcase
        case(reg_source_2_data_sel)
            2'b10: reg_source_2_data = reg_data_wb;
            2'b01: reg_source_2_data = reg_data_dm;
            2'b00: reg_source_2_data = reg_source_2_data_ex;
            default: reg_source_2_data = reg_source_2_data_ex;
        endcase
    end

    assign alu_port_a_ex = execute_port_a_sel_ex ? reg_source_1_data : PC_ex;
    assign alu_port_b_ex = execute_port_b_sel_ex ? reg_source_2_data : immediate_ex;
    assign comp_port_a_ex = reg_source_1_data;
    assign comp_port_b_ex = execute_port_b_sel_ex ? immediate_ex : reg_source_2_data;

    ArithmeticLogicUnit #(.XLEN(XLEN), .REG_ADDR_WIDTH(REG_ADDR_WIDTH)) alu
    (
        .alu_o          (alu_output_ex),
        .alu_port_a_i   (alu_port_a_ex),
        .alu_port_b_i   (alu_port_b_ex),
        .alu_op_sel_i   (alu_op_sel_ex)
    );

    ComparatorUnit #(.XLEN(XLEN)) bcu
    (
        .comp_o           (comp_output_ex),
        .comp_port_a_i    (comp_port_a_ex),
        .comp_port_b_i    (comp_port_b_ex),
        .comp_op_sel_i    (comp_op_sel_ex)
    );

///////////////////////////////////////////////////////////////
//             EXECUTE / DATA MEMORY INTERFACE               //
///////////////////////////////////////////////////////////////

    always_ff @(posedge clk_i)
    begin
        pc_next_dm <= pc_next_ex;
        reg_destination_addr_dm <= reg_destination_addr_ex;
        immediate_dm <= immediate_ex;
        reg_source_2_data_dm <= reg_source_2_data;
        write_data_unaligned_dm <= reg_source_2_data;
        alu_output_dm <= alu_output_ex;
        comp_output_dm <= comp_output_ex;
        load_store_type_dm <= load_store_type_ex;
        reg_write_enable_dm <= reg_write_enable_ex;
        data_memory_write_enable_dm <= data_memory_write_enable_ex;
        reg_write_data_sel_dm <= reg_write_data_sel_ex;
    end

///////////////////////////////////////////////////////////////
//                   DATA MEMORY STAGE                       //
///////////////////////////////////////////////////////////////

    LoadAndStoreUnit #(.XLEN(XLEN)) lsu
    (
        .read_data_o         (read_data_aligned_dm),
        .read_data_i         (read_data_i),
        .write_data_o        (write_data_o),
        .write_data_i        (write_data_unaligned_dm),
        .load_store_type_i   (load_store_type_dm),
        .write_data_strobe_o (write_data_strobe_o)
    );

    assign address_o = alu_output_dm;
    assign dmem_data_dm = read_data_aligned_dm;
    assign write_enable_o = data_memory_write_enable_dm;
    assign write_data_unaligned = reg_source_2_data_dm;

    always_comb
    begin
        unique case(reg_write_data_sel_dm)
             risc_v_32_i_pkg::RD_MUX_ALU:    reg_data_dm = alu_output_dm;
             risc_v_32_i_pkg::RD_MUX_BCU:    reg_data_dm = {31'b0, comp_output_dm};
             risc_v_32_i_pkg::RD_MUX_IMM:    reg_data_dm = immediate_dm;
            risc_v_32_i_pkg::RD_MUX_PC_N:    reg_data_dm = pc_next_dm;
                default:    reg_data_dm = {XLEN{1'bz}};
        endcase
    end

///////////////////////////////////////////////////////////////
//             DATA MEMORY / WRITE BACK INTERFACE            //
///////////////////////////////////////////////////////////////

    always_ff @(posedge clk_i)
    begin
        reg_destination_addr_wb <= reg_destination_addr_dm;
        reg_write_enable_wb <= reg_write_enable_dm;
        reg_write_data_sel_wb <= reg_write_data_sel_dm;
        dmem_data_wb <= dmem_data_dm;
        reg_data <= reg_data_dm;
    end

///////////////////////////////////////////////////////////////
//                    WRITE BACK STAGE                       //
///////////////////////////////////////////////////////////////

    assign reg_data_wb = (reg_write_data_sel_wb == risc_v_32_i_pkg::RD_MUX_DMEM) ? dmem_data_wb : reg_data;

///////////////////////////////////////////////////////////////
//               PIPELINE DATA / CONTROL                     //
///////////////////////////////////////////////////////////////

PipelineControlUnit #(.REG_ADDR_WIDTH(REG_ADDR_WIDTH)) pcu
(
    .stall_pc_if_o                  (stall_pc_if),
    .stall_if_id_o                  (stall_if_id),
    .clear_if_id_o                  (clear_if_id),
    .clear_id_ex_o                  (clear_id_ex),
    .reg_source_1_data_sel_o        (reg_source_1_data_sel),
    .reg_source_2_data_sel_o        (reg_source_2_data_sel),
    .reg_source_1_addr_id_i         (reg_source_1_addr_id),
    .reg_source_2_addr_id_i         (reg_source_2_addr_id),
    .reg_source_1_addr_ex_i         (reg_source_1_addr_ex),
    .reg_source_2_addr_ex_i         (reg_source_2_addr_ex),
    .reg_destination_addr_ex_i      (reg_destination_addr_ex),
    .reg_destination_addr_dm_i      (reg_destination_addr_dm),
    .reg_destination_addr_wb_i      (reg_destination_addr_wb),
    .reg_write_enable_dm_i          (reg_write_enable_dm),
    .reg_write_enable_wb_i          (reg_write_enable_wb),
    .reg_write_data_sel_ex_i        (reg_write_data_sel_ex),
    .branch_enable_i                (branch_enable)
);

endmodule
